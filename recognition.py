from __future__ import annotations

from datetime import datetime
import enum
import functools
import json
import logging
import os
import ssl
from urllib.error import URLError, HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

import boto3
from botocore.exceptions import ClientError

# Recognition table state constants:
# - Presigned URL created, but file was not uploaded yet
STATUS_AWAITING_UPLOAD = 'AWAITING_UPLOAD'
# - Rekognition successfully recognized the image
STATUS_RECOGNITION_FINISHED = 'SUCCESSFUL_RECOGNITION'
# - The provided file was already recognized recently, and the result was extracted
#   from the cache
STATUS_RECOGNITION_CACHED = 'SUCCESSFUL_CACHED'
# - Rekognition couldn't recognize image (or something broke in the wrapper)
STATUS_RECOGNITION_FAILED = 'FAILED_RECOGNITION'
# - The provided file was rejected by Rekognition before
STATUS_RECOGNITION_CACHED_FAILURE = 'FAILED_CACHED'

RECOGNITION_CACHE_LIFETIME = int(os.environ['RECOGNITION_CACHE_LIFETIME'])
RECOGNITION_CALLBACK_TIMEOUT = int(os.environ['RECOGNITION_CALLBACK_TIMEOUT'])

REKOGNITION_API_MAX_FILE_SIZE = int(os.environ['REKOGNITION_API_MAX_FILE_SIZE'])

JPEG_HEADER = b'\xff\xd8\xff'
JPEG_FOOTER = b'\xff\xd9'
PNG_HEADER = b'\x89\x50\x4e\x47'

class PrevalInvalidImageFormatException(Exception):
    pass

class RecognitionService:
    def __init__(self, s3, ddb, rekognition):
        self._s3 = s3
        self._ddb = ddb
        self._rekognition = rekognition

        self._ddb_tasks_table = self._ddb.Table(
            os.environ['DD_RECOGNITION_TASKS_TABLE'])
        self._ddb_cache_table = None

    def create_blob(self, callback_url: str = None,
                    allow_insecure_callback: bool = False) -> tuple[str, dict]:
        ''' Generates a random blob id, pre-signed S3 upload URL for recognition
        bucket and saves them to the recognition table.

        Args:
            callback_url (optional): URL to report the recognition result to.
            allow_insecure_callback (optional): When true, certificate validity
                won't be checked for https callbacks.

        Returns:
            tuple[str, dict]: generated blob id and presigned URL
                (with request body to send to S3)
        '''
        blob_id = str(uuid4())

        presigned_url = self._s3.generate_presigned_post(
            os.environ['S3_RECOGNITION_BUCKET'], blob_id, Conditions=[
                ['content-length-range', 0, REKOGNITION_API_MAX_FILE_SIZE]
            ], ExpiresIn=3600)

        item = {
            'blobId': blob_id,
            'status': STATUS_AWAITING_UPLOAD,
            'timestamp': int(datetime.now().timestamp())}

        if callback_url is not None:
            item['callback_url'] = callback_url
            item['allow_insecure_callback'] = allow_insecure_callback

        logger.info("Generated blob '%s'", blob_id)

        self._ddb_tasks_table.put_item(Item=item)
        return blob_id, presigned_url

    def _update_status(self, blob_id: str, status: str, result: str = None,
                       error: str = None) -> None:
        '''Updates recognition table items.

        Args:
            blob_id: ID of the table item to create/update.
            status: Status to be assigned to the entry.
            result: When not `None`, will override the existing `result` field.
            error: When not `None`, will override the existing `error` field.
        '''

        timestamp = int(datetime.now().timestamp())

        # As DynamoDB has reserved words that overlap with our schema,
        # we'll need to replace them with expression attributes
        attribute_names = {'#s': 'status', '#t': 'timestamp'}
        expression = 'SET #s=:s, #t=:t'
        values = {':s': status, ':t': timestamp}

        if result is not None:
            expression += ', #r=:r'
            values[':r'] = result
            attribute_names['#r'] = 'result'

        if error is not None:
            expression += ', #e=:e'
            values[':e'] = error
            attribute_names['#e'] = 'error'

        self._ddb_tasks_table.update_item(
            Key={'blobId': blob_id},
            UpdateExpression=expression,
            ExpressionAttributeNames=attribute_names,
            ExpressionAttributeValues=values)

        logger.info("Updated blob '{%s}' with status '{%s}', "
            + "error '{%s}', result '{%s}'", blob_id, status, error, result)

    def _set_status_from_cache(self, blob_id: str, etag: str, now: int) -> bool:
        ''' Updates blob_id status with cached one (if exists and applicable)

        Successful responses cache has limited lifetime and will stale with time.
        Cached failures caused by unsupported/broken file will always be returned.

        Args:
            blob_id: ID of the object in recognition DynamoDB table.
            etag: eTag value of the blob.
            now: the current unix epoch time.

        Returns:
            bool: whether cache was hit or not
        '''
        if self._ddb_cache_table is None:
            self._ddb_cache_table = self._ddb.Table(
                os.environ['DD_RECOGNITION_CACHE_TABLE'])

        # Find recognition results in cache
        cached = self._ddb_cache_table.get_item(Key={'etag': etag})
        if 'Item' in cached and 'result' in cached['Item']:
            item = cached['Item']
            # Cached errors indicate that the file is not analyzable,
            # no matter how stale cache is. No further action required.
            if 'error' in item:
                self._update_status(blob_id,
                                    STATUS_RECOGNITION_CACHED_FAILURE,
                                    error=item['error'])
                return True
            # If results are not stale, update recognition status and return
            if now - item['timestamp'] < RECOGNITION_CACHE_LIFETIME:
                self._update_status(blob_id, STATUS_RECOGNITION_CACHED,
                                    result=item['result'])
                return True
        return False

    def process_blob(self, blob_id: str, bucket: str, etag: str) -> None:
        ''' Handles the uploaded blob

        The method returns cached recognition result if a blob with the same 
        `etag` was recognized recently. Otherwise, it tries to run Rekognition
        on the given blob, then caches the results and updates the recognition 
        table.

        Args:
            blob_id: ID of the object in the provided S3 bucket (must match 
                existing blob_id in recognition DynamoDB table).
            bucket: ID of the S3 bucket that contains the blob with `blob_id`.
            etag: eTag value of the blob.
        '''
        timestamp = int(datetime.now().timestamp())

        error = None
        should_cache_error = False
        try:
            cache_hit = self._set_status_from_cache(blob_id, etag, timestamp)
            if cache_hit:
                logger.info("Cache hit for blob '%s'", blob_id)
                return

            # Cache miss, prevalidate file before calling Rekognition

            # Check file header
            file_header = self._s3.get_object(
                Bucket=bucket, Key=blob_id, Range="bytes=0-3")['Body'].read()

            # Rekognition accepts JPEG and PNG files only.
            #
            # Note: this check will filter out RARJPEGs and other steganographic
            # techniques that involve gluing multiple files of different formats
            # together.
            if file_header[:3] == JPEG_HEADER:
                file_footer = self._s3.get_object(
                    Bucket=bucket, Key=blob_id, Range="bytes=-2")['Body'].read()
                if file_footer[-2:] != JPEG_FOOTER:
                    raise PrevalInvalidImageFormatException()
            elif file_header != PNG_HEADER:
                raise PrevalInvalidImageFormatException()

            # Request labels from Rekognition
            result = self._rekognition.detect_labels(Image={'S3Object': {
                'Bucket': bucket,
                'Name': blob_id}})['Labels']

            # Save result to the recognition table
            self._update_status(blob_id, STATUS_RECOGNITION_FINISHED,
                                result=json.dumps(result))
            # Save result to the cache
            self._ddb_cache_table.put_item(Item={'etag': etag,
                                                 'timestamp': timestamp,
                                                 'result': json.dumps(result)})
            # Delete recognized blob
            self._s3.delete_object(Bucket=bucket, Key=blob_id)
            return

        except (self._rekognition.exceptions.InvalidImageFormatException,
                PrevalInvalidImageFormatException):
            error = '415 Invalid image format'
            should_cache_error = True  # The file won't become an image
        except self._rekognition.exceptions.ImageTooLargeException:
            error = '400 Image too large'
            should_cache_error = True  # The file won't become smaller
        except (self._rekognition.exceptions.ProvisionedThroughputExceededException,
                self._rekognition.exceptions.ThrottlingException):
            # Rate limit errors should not be cached...
            error = '429 Try again later'
        except ClientError:
            # ...as well as unexpected faults
            error = '500 Internal server error'

        if error is not None:
            self._update_status(
                blob_id, STATUS_RECOGNITION_FAILED, error=error)
            if should_cache_error:
                self._ddb_cache_table.put_item(Item={
                    'etag': etag,
                    'timestamp': timestamp,
                    'error': error})

    def call_back(self, blob_id: str, callback_url: str, status: str,
                  result: str = None, error: str = None,
                  allow_insecure_callback: bool = False) -> None:
        ''' Sends the callback to the specified URL '''
        payload = {
            'blob_id': blob_id,
            'status': status
        }
        if result is not None:
            payload['result'] = json.loads(result)
        if error is not None:
            payload['error'] = error

        req = Request(callback_url, method='POST',
                      headers={
                          'Content-Type': 'application/json',
                          'User-Agent': os.environ['RECOGNITION_USER_AGENT']},
                      data=json.dumps(payload).encode('utf-8'))

        error = None
        try:
            ssl_context = ssl.create_default_context()
            if allow_insecure_callback:
                ssl_context.verify_mode = ssl.CERT_NONE
                ssl_context.check_hostname = False
            resp = urlopen(req, context=ssl_context, 
                           timeout=RECOGNITION_CALLBACK_TIMEOUT)
        except HTTPError as e:
            error = 'Server responded with code {}'.format(e.code)
        except URLError as e:
            if isinstance(e.reason, ssl.SSLError):
                error = "Failed SSL verification, consider using 'allow_insecure_callback'"
            else:
                error = 'Failed to connect to the callback_url server'
        except:
            error = 'General error while calling back'

        if error is not None:
            self._ddb_tasks_table.update_item(
                Key={'blobId': blob_id},
                UpdateExpression='SET callback_error=:cbe',
                ExpressionAttributeValues={':cbe': error})


logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client('s3')
ddb = boto3.resource('dynamodb')
rekognition = boto3.client('rekognition')
blobs_table = ddb.Table(os.environ['DD_RECOGNITION_TASKS_TABLE'])

service = RecognitionService(s3, ddb, rekognition)


def make_response(code: int, body: dict) -> str:
    return {
        'statusCode': code,
        'headers': {
            'content-type': 'application/json'
        },
        'body': json.dumps(body)
    }


def create_blob(event, context):
    ''' Lambda entry point for create_blob '''
    # Note: for bigger projects, I'd probably use pydantic for validation.
    # However, as this is the only place I need to validate mere two fields,
    # I do it manually to save some lambda execution time.

    url = None
    insecure = False

    # Check if event body has request details
    if 'content-type' in event['headers'] and 'body' in event and event['body']:
        # Filter Content-Type
        if event['headers']['content-type'] != 'application/json':
            return make_response(400, {
                'error': 'This endpoint accepts application/json only.'
            })

        # Validate JSON body correctness
        try:
            body = json.loads(event['body'])
        except json.JSONDecodeError:
            return make_response(400, {
                'error': 'Unable to parse body as JSON, please check your request.'
            })

        # Filter unknown keys
        expected_fields = ('callback_url', 'allow_insecure_callback')
        if any([x not in expected_fields for x in body.keys()]):
            return make_response(400, {
                'error': 'Your request contains unknown keys, please make sure '
                    + 'only ({}) fields are present'.format(', '.join(expected_fields))
            })

        # Extract data from JSON body
        if 'callback_url' in body:
            url = body['callback_url']
            if not isinstance(url, str):
                return make_response(400, {
                    'error': 'callback_url should be a string.'
                })
            parsed = urlparse(url)
            if parsed.scheme != 'http' and parsed.scheme != 'https':
                return make_response(400, {
                    'error': 'callback_url only supports http and https protocols, ' \
                        + "please make sure your callback URL starts with 'http://' or 'https://'."
                })

            if not parsed.netloc:
                return make_response(400, {
                    'error': 'Invalid callback_url, please check your request.'
                })

            insecure = body.get('allow_insecure_callback', False)
            if not isinstance(insecure, bool):
                return make_response(400, {
                    'error': 'allow_insecure_callback should be a boolean.'
                })

    blob_id, presign_url_data = service.create_blob(url, insecure)

    response = make_response(200, {
        'blob_id': blob_id,
        'upload_info': presign_url_data
    })

    return response


def process_blob(event, context):
    ''' Lambda entry point for process_blob '''
    for record in event['Records']:
        service.process_blob(
            record['s3']['object']['key'],
            record['s3']['bucket']['name'],
            record['s3']['object']['eTag'])


def make_callback(event, context):
    ''' Lambda entry point for make_callback '''
    for record in event['Records']:
        obj = record['dynamodb']['NewImage']
        if 'callback_url' in obj:
            blob_id = obj['blobId']['S']
            callback_url = obj['callback_url']['S']
            status = obj['status']['S']
            result = obj['result']['S'] if 'result' in obj else None
            error = obj['error']['S'] if 'error' in obj else None
            insecure = 'allow_insecure_callback' in obj \
                and obj['allow_insecure_callback']['BOOL']
            service.call_back(blob_id, callback_url, status,
                              result, error, insecure)

def fetch_blob_info(event, context):
    ''' Lambda that fetches the blob from DynamoDB, replacing the costly 
    AWS REST API with Lambda + HTTP API '''
    if 'pathParameters' not in event or not event['pathParameters']['blobId']:
        return make_response(400, {
            "error": "blob id is missing"
        })
    blob_id = event['pathParameters']['blobId']
    response = blobs_table.get_item(Key={'blobId': blob_id})
    if 'Item' not in response:
        return make_response(404, {
            "error": "not found"
        })
    blob_item = response['Item']

    del blob_item['timestamp']
    if 'allow_insecure_callback' in blob_item:
        del blob_item['allow_insecure_callback']
    if 'result' in blob_item and isinstance(blob_item['result'], str):
        blob_item['result'] = json.loads(blob_item['result'])

    return make_response(200, blob_item)
