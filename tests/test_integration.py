from __future__ import annotations

from datetime import datetime, timedelta
import json
import os.path
import pathlib

import pytest
import requests

# TODO Fetch this dynamically
root_url = "https://px5764ykx6.execute-api.us-east-1.amazonaws.com"

def get_url(path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    return root_url + path

def create_blob() -> tuple[str, str]:
    result = requests.post(get_url("/blobs")).json()
    return result['blob_id'], result['upload_info']

def get_blob_info(blob_id: str) -> dict:
    return requests.get(get_url("/blobs/{}".format(blob_id))).json()

def send_blob(filepath: str) -> dict:
    blob_id, upload_info = create_blob()

    with open(filepath, 'rb') as f:
        result = requests.post(upload_info['url'], data=upload_info['fields'], files={
            "file": f
        })
        if result.status_code != 204:
            raise RuntimeError("Wrong response code")

    started_at = datetime.now()

    timeout = timedelta(seconds=30)
    while datetime.now() - started_at < timeout:
        blob_info = get_blob_info(blob_id)
        if blob_info['status'] != 'AWAITING_UPLOAD':
            return blob_info
    raise TimeoutError()

def case_file_path(filename: str) -> str:
    return os.path.join(pathlib.Path(__file__).parent.absolute(), 'cases', filename)

def success(filename: str) -> None:
    result = send_blob(case_file_path(filename))
    assert result['status'].startswith('SUCCESSFUL')
    assert 'error' not in result

@pytest.mark.integration
def test_success_1():
    success('test1.jpeg')

@pytest.mark.integration
def test_success_2():
    success('test2.png')

@pytest.mark.integration
def test_success_3():
    success('test3.jpeg')

@pytest.mark.integration
def test_success_4():
    success('test4.JPG')

@pytest.mark.integration
def test_success_5():
    success('test5.jpg')

@pytest.mark.integration
def test_invalid_file_type():
    result = send_blob(case_file_path('random_blob.txt'))
    assert result['status'].startswith('FAILED')
    assert 'error' in result
    assert result['error'].startswith('415')

@pytest.mark.integration
def test_create_blob_callback_http_success():
    result = requests.post(get_url("/blobs"), json={
        "callback_url": "http://example.com/"
    }).json()
    assert 'blob_id' in result
    assert 'upload_info' in result

@pytest.mark.integration
def test_create_blob_callback_https_success():
    result = requests.post(get_url("/blobs"), json={
        "callback_url": "https://example.com/"
    }).json()
    assert 'blob_id' in result
    assert 'upload_info' in result

@pytest.mark.integration
def test_create_blob_callback_wrong_schema_failure():
    result = requests.post(get_url("/blobs"), json={
        "callback_url": "ftp://example.com/"
    }).json()
    assert 'error' in result

@pytest.mark.integration
def test_create_blob_wrong_url_failure():
    result = requests.post(get_url("/blobs"), json={
        "callback_url": "https://"
    }).json()
    assert 'error' in result

@pytest.mark.integration
def test_create_blob_empty_json_success():
    result = requests.post(get_url("/blobs"), json={}).json()
    assert 'blob_id' in result
    assert 'upload_info' in result

@pytest.mark.integration
def test_create_blob_insecure_callback_not_boolean_failure():
    result = requests.post(get_url("/blobs"), json={
        "callback_url": "https://example.com/",
        "allow_insecure_callback": 4242
    }).json()
    assert 'error' in result

@pytest.mark.integration
def test_create_blob_not_json_failure():
    result = requests.post(get_url("/blobs"), data={
        "callback_url": "https://example.com/"
    }).json()
    assert 'error' in result
