from __future__ import annotations

import os.path
import pathlib
from datetime import datetime, timedelta

import requests

# TODO Fetch this dynamically
root_url = "https://px5764ykx6.execute-api.us-east-1.amazonaws.com"


def get_url(path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    return root_url + path


def create_blob(data: dict = {}) -> tuple[str, str]:
    """ Creates a blob on the service """
    result = requests.post(get_url("/blobs"), json=data).json()
    return result['blob_id'], result['upload_info']


def get_blob_info(blob_id: str) -> dict:
    """ Fetches blob data from the service """
    return requests.get(get_url("/blobs/{}".format(blob_id))).json()


def wait_for_analysis(blob_id: str, timeout: int = 30):
    """ Waits for blob to be uploaded and processed """
    started_at = datetime.now()
    timeout = timedelta(seconds=timeout)
    while datetime.now() - started_at < timeout:
        blob_info = get_blob_info(blob_id)
        if blob_info['status'] != 'AWAITING_UPLOAD':
            return blob_info
    raise TimeoutError()


def analyze_image(filepath: str, data: dict = {}) -> str:
    """ Analyzes image from given file """
    blob_id, upload_info = create_blob(data=data)

    with open(filepath, 'rb') as f:
        result = requests.post(upload_info['url'], data=upload_info['fields'], files={
            "file": f
        })
        if result.status_code != 204:
            raise RuntimeError("Wrong response code")

    return blob_id, wait_for_analysis(blob_id)


def case_file_path(filename: str) -> str:
    """ Provides absolute path to the test case file """
    return os.path.join(pathlib.Path(__file__).parent.absolute(), 'cases', filename)
