from __future__ import annotations

import json
import os.path
import pathlib
from datetime import datetime, timedelta
from time import sleep

import pytest
import requests

from integration_utils import (case_file_path, get_url, analyze_image,
                               wait_for_analysis, get_blob_info)


@pytest.fixture(scope="module")
def webhook_uuid() -> str:
    """ Fixture that generates webhook on webhook.site and returns its UUID """
    uuid = requests.post("https://webhook.site/token").json()['uuid']
    yield uuid
    requests.delete("https://webhook.site/token/{}".format(uuid))


@pytest.fixture(scope="module")
def webhook_url(webhook_uuid: str) -> str:
    """ Fixture that provides URL to call back to """
    return "https://webhook.site/{}".format(webhook_uuid)


@pytest.fixture(scope="module")
def webhook_requests_url(webhook_uuid: str) -> list[dict]:
    """ Fixture that provides URL to fetch requests with """
    return "https://webhook.site/token/{}/requests".format(webhook_uuid)


def get_webhook_requests(url: str) -> list[dict]:
    """ Fetches paginated list of webhook requests """
    result = []

    is_last_page = False
    while not is_last_page:
        page = requests.get(url).json()
        result += page['data']
        is_last_page = page['is_last_page']

    return result


def find_webhook_request(blob_id: str, webhook_requests: list[dict]) -> dict:
    """ Returns webhook request data related to the blob with provided ID """
    for r in webhook_requests:
        content = json.loads(r['content'])
        if blob_id == content['blob_id']:
            return content


@pytest.mark.integration
@pytest.mark.webhook
def test_webhook_recognition_success(webhook_url, webhook_requests_url):
    blob_id, _ = analyze_image(case_file_path("test1.jpeg"), {
        "callback_url": webhook_url
    })

    sleep(5)

    callback_info = find_webhook_request(
        blob_id, get_webhook_requests(webhook_requests_url))

    assert callback_info is not None
    assert callback_info['status'].startswith("SUCCESSFUL")
    assert 'error' not in callback_info


@pytest.mark.integration
@pytest.mark.webhook
def test_webhook_recognition_fail(webhook_url, webhook_requests_url):
    blob_id, _ = analyze_image(case_file_path("random_blob.txt"), {
        "callback_url": webhook_url
    })

    sleep(5)

    callback_info = find_webhook_request(
        blob_id, get_webhook_requests(webhook_requests_url))

    assert callback_info is not None
    assert callback_info['status'].startswith("FAILED")


@pytest.mark.integration
@pytest.mark.webhook
def test_webhook_wrong_url(webhook_url, webhook_requests_url):
    blob_id, _ = analyze_image(case_file_path("test1.jpeg"), {
        "callback_url": "https://nxdomain.nxtld"
    })

    sleep(5)

    result = get_blob_info(blob_id)
    assert 'callback_error' in result
