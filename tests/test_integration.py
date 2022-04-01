from __future__ import annotations

import json

import pytest
import requests

from integration_utils import case_file_path, get_url, analyze_image


def success(filename: str) -> None:
    _, result = analyze_image(case_file_path(filename))
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
    _, result = analyze_image(case_file_path('random_blob.txt'))
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
