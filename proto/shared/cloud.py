"""The two things every service needs from Google Cloud, with a laptop stand-in for each.

publish(topic, message)
    gcp:   Pub/Sub publish. Push subscriptions deliver it to the subscriber.
    local: POST the same push envelope straight to the subscriber, in the
           background, so it behaves like an asynchronous queue.

call(method, url, ...)
    gcp:   adds a Google-signed identity token, so internal Cloud Run services
           only accept calls from identities that are allowed to invoke them.
    local: plain HTTP.
"""

import base64
import json
import logging
import os
import threading
import uuid

import httpx

RUN_MODE = os.getenv("RUN_MODE", "local")
PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "")
log = logging.getLogger("cloud")

# Laptop only: where a push subscription would deliver each topic.
LOCAL_SUBSCRIBERS = {
    "stockout-events": os.getenv("AGENT_URL", "http://localhost:8080") + "/pubsub/stockouts",
    "customer-offers": None,  # nobody subscribes on the laptop; the demo screen shows offers
}


def _google_session():
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return AuthorizedSession(credentials)


def google_api(method: str, url: str, **kwargs) -> dict:
    """Call a Google API (Pub/Sub, BigQuery, Vertex AI Search) as this service's identity."""
    r = _google_session().request(method, url, timeout=15, **kwargs)
    r.raise_for_status()
    return r.json()


def publish(topic: str, message: dict) -> None:
    data = base64.b64encode(json.dumps(message).encode()).decode()
    if RUN_MODE == "gcp":
        google_api("POST", f"https://pubsub.googleapis.com/v1/projects/{PROJECT}/topics/{topic}:publish",
                   json={"messages": [{"data": data}]})
        return
    url = LOCAL_SUBSCRIBERS[topic]
    if url:
        envelope = {"message": {"data": data, "messageId": uuid.uuid4().hex}}
        threading.Thread(target=_deliver, args=(url, envelope), daemon=True).start()


def _deliver(url: str, envelope: dict) -> None:
    try:
        httpx.post(url, json=envelope, timeout=90)
    except httpx.HTTPError as e:
        log.warning("local delivery to %s failed: %s", url, e)


def read_push(envelope: dict) -> dict:
    """Unwrap a Pub/Sub push request body."""
    return json.loads(base64.b64decode(envelope["message"]["data"]))


def auth_headers(url: str) -> dict:
    if RUN_MODE != "gcp":
        return {}
    import google.oauth2.id_token
    from google.auth.transport.requests import Request
    audience = "/".join(url.split("/")[:3])  # https://service-host
    return {"Authorization": "Bearer " + google.oauth2.id_token.fetch_id_token(Request(), audience)}
