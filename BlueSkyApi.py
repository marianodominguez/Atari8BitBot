
from typing import Self

from atproto import Client, client_utils
from atproto_client.client.session import get_session_pds_endpoint
from atproto_client import models
from bs4 import BeautifulSoup as bs
import logging
import re
import os
import datetime
from zoneinfo import ZoneInfo
import httpx


from types import SimpleNamespace

from requests import session

class BlueSkyApi:
    logger = logging.getLogger(__name__)
    api = None

    def get_upload_limits(Self):
        pds_did = Self._get_pds_did()

        def _request_limits_with_aud(aud: str):
            token = Self.api.com.atproto.server.get_service_auth(
                params=models.ComAtprotoServerGetServiceAuth.Params(
                    aud=aud,
                    lxm='app.bsky.video.getUploadLimits',
                    exp=int(datetime.datetime.now(tz=ZoneInfo("UTC")).timestamp()) + 60 * 30,
                )
            ).token

            url = "https://video.bsky.app/xrpc/app.bsky.video.getUploadLimits"
            headers = {"Authorization": f"Bearer {token}"}
            Self.logger.debug(f"Requesting upload limits with aud={aud}")
            return httpx.get(url, headers=headers, timeout=30)

        # Try with the PDS-derived did first
        resp = _request_limits_with_aud(pds_did)

        # If the video service rejects the token (401), retry using the well-known
        # video service DID as the audience. Some deployments expect aud=did:web:video.bsky.app.
        if resp.status_code == 401 and pds_did != "did:web:video.bsky.app":
            Self.logger.warning("Upload limits request returned 401; retrying with did:web:video.bsky.app as aud")
            resp = _request_limits_with_aud("did:web:video.bsky.app")

        if resp.status_code != 200:
            # Try to parse error body; some 401 responses include structured JSON
            try:
                body = resp.json()
            except Exception:
                Self.logger.error(f"Failed to get upload limits: {resp.status_code} {resp.text}")
                resp.raise_for_status()

            # If the service explicitly reports daily upload limit exceeded, return
            # the body instead of raising so callers can continue running and
            # decide how to behave (e.g., skip uploads or sleep).
            if isinstance(body, dict) and body.get("error") == "daily_vid_limit_exceeded":
                Self.logger.warning(f"Daily upload limit exceeded: {body.get('message')}")
                return body

            Self.logger.error(f"Failed to get upload limits: {resp.status_code} {resp.text}")
            resp.raise_for_status()

        Self.logger.debug("Successfully retrieved upload limits")
        return resp.json()

    def get_api(Self, client_id, client_secret, access_token, access_token_secret):
        c=Client()
        c.login(client_id, client_secret)
        Self.api=c

        session = Self.api.com.atproto.server.get_session()
        Self.logger.info(f"Email confirmed: {session.email_confirmed}")

        limits = Self.get_upload_limits()
        Self.logger.info(f"Upload limits: {limits}")

        return Self.api

    def media_upload(Self,filename):
        if not os.path.exists(filename):
            raise FileNotFoundError(f"Video file does not exist: {filename}")
        file_size = os.path.getsize(filename)
        if file_size == 0:
            raise ValueError(f"Video file is empty: {filename}")
        Self.logger.debug(f"Validated video file {filename} ({file_size} bytes)")
        return filename

    def _get_pds_did(Self):
        from atproto_client.client.session import get_session_pds_endpoint
        endpoint = get_session_pds_endpoint(Self.api._session)
        host = endpoint.split('://')[1].rstrip('/')
        return f"did:web:{host}"

    def update_status(Self, text, media, msg):
        post = msg.post
        this_parent = models.create_strong_ref(post)
        this_root = models.create_strong_ref(post)

        with open(media, 'rb') as f:
            vid_data = f.read()

        pds_did = Self._get_pds_did()

        # 1. Service auth token — lxm is com.atproto.repo.uploadBlob (the video
        #    service just proxies the upload through to your PDS as a blob)
        token = Self.api.com.atproto.server.get_service_auth(
            params=models.ComAtprotoServerGetServiceAuth.Params(
                aud=pds_did,
                lxm='com.atproto.repo.uploadBlob',
                exp=int(datetime.datetime.now(tz=ZoneInfo("UTC")).timestamp()) + 60 * 30,
            )
        ).token

        # 2. Plain HTTP POST to the video service — no SDK Client needed here
        upload_url = "https://video.bsky.app/xrpc/app.bsky.video.uploadVideo"
        params = {"did": Self.api.me.did, "name": os.path.basename(media)}
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "video/mp4",
        }

        resp = httpx.post(upload_url, params=params, headers=headers, content=vid_data, timeout=60)
        if resp.status_code == 409:
            job_status = resp.json()
            if job_status.get("error") == "already_exists":
                Self.logger.warning(
                    f"Video upload conflict ({resp.status_code}): already exists; reusing existing job {job_status.get('jobId')}")
            else:
                Self.logger.error(f"Video upload failed ({resp.status_code}): {resp.text}")
                resp.raise_for_status()
        elif resp.status_code != 200:
            Self.logger.error(f"Video upload failed ({resp.status_code}): {resp.text}")
            resp.raise_for_status()
        else:
            job_status = resp.json()
        Self.logger.debug(f"Video job started: {job_status}")

        # 3. Poll for completion via the video service endpoint.
        import time
        job_id = job_status["jobId"]
        state = job_status.get("state")
        blob = job_status.get("blob")

        def request_job_status(job_id: str):
            token = Self.api.com.atproto.server.get_service_auth(
                params=models.ComAtprotoServerGetServiceAuth.Params(
                    aud=pds_did,
                    lxm='app.bsky.video.getJobStatus',
                    exp=int(datetime.datetime.now(tz=ZoneInfo("UTC")).timestamp()) + 60 * 30,
                )
            ).token
            status_url = "https://video.bsky.app/xrpc/app.bsky.video.getJobStatus"
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            resp = httpx.get(status_url, params={"jobId": job_id}, headers=headers, timeout=30)
            if resp.status_code != 200:
                Self.logger.error(f"Video job status failed ({resp.status_code}): {resp.text}")
                resp.raise_for_status()
            result = resp.json()
            return result.get("jobStatus") or result.get("job_status") or result

        while not blob and state not in ("JOB_STATE_FAILED",):
            time.sleep(2)
            status = request_job_status(job_id)
            state = getattr(status, 'state', status.get('state')) if isinstance(status, dict) else status.state
            blob = getattr(status, 'blob', status.get('blob')) if isinstance(status, dict) else status.blob
            Self.logger.debug(f"Job status: {state}")

        if not blob:
            raise RuntimeError(f"Video transcoding failed: state={state}")

        # 4. Post with the fully-processed blob
        embed = models.AppBskyEmbedVideo.Main(video=blob, alt='')
        return Self.api.send_post(
            text=text,
            embed=embed,
            reply_to=models.AppBskyFeedPost.ReplyRef(parent=this_parent, root=this_root),
        )

    def reply(Self, status, text):
        msg=" "
        post=status.post
        for line in text.split("\n"):
            if "ERROR"  in line or "error:" in line:
                msg=msg+line+"\n"

        Self.logger.info(f"MSG: {msg}")

        this_parent = models.create_strong_ref(post)
        this_root = models.create_strong_ref(post)

        status = {}
        try:
            status = Self.api.send_post(
                text=msg,
                reply_to=models.AppBskyFeedPost.ReplyRef(parent=this_parent, root=this_root))
        except:
            Self.logger.error(f"Unable to post message: {status}")

    def get_replies(Self, since_id):
        replies={}
        result = []
        since_date= datetime.datetime.fromtimestamp(since_id/1000, tz=ZoneInfo("UTC"))

        Self.logger.info(since_date.isoformat(timespec='milliseconds'))

        response = Self.api.app.bsky.feed.search_posts(
            params = models.AppBskyFeedSearchPosts.Params(
                q="#atari8bitbot",
                since=since_date.isoformat(timespec='milliseconds')
            )
        )
        result.extend(response.posts)

        Self.logger.debug(f"result: {result}")
        for post in result:
            # parse the message to extract entities
            message = Self.extract_entities(post.record.text)
            status = SimpleNamespace()
            status.post = post

            # created_at -> datetime -> milliseconds
            ts = datetime.datetime.fromisoformat(post.record.created_at)
            post_ms = int(ts.timestamp() * 1000)

            # If the search API returned posts older than since_id, skip them.
            if post_ms <= since_id:
                Self.logger.debug(f"Skipping post older-or-equal to since_id: post_ms={post_ms} since_id={since_id}")
                continue

            # offset 100 milliseconds to avoid getting the same message
            status.id = int(post_ms + 100)
            # status.id = post.cid
            status.entities = {}
            if 'urls' in message.keys():
                if 'urls' not in status.entities.keys():
                    status.entities['urls'] = []
                status.entities['urls'] = message['urls']
            status.user = SimpleNamespace()
            status.user.screen_name = post.author.display_name
            status.user.name = post.author.handle
            status.full_text = message['text'].strip()
            replies[status.id] = status
            Self.logger.debug(f"status: {status.id}")
        Self.logger.info(f"replies: {replies}")
        return replies.values()

    def extract_entities(Self,html_doc):
        message={}
        html_doc=re.sub(r'<br\s*/?>', '\n', html_doc)
        html_doc=re.sub(r'</p>', '\n', html_doc)
        html_doc=re.sub('<[^<]+?>', '', html_doc)
        html_doc=re.sub('#atari8bitbot\s?', '', html_doc, flags=re.IGNORECASE)
        soup = bs(html_doc, 'html.parser')
        message['text'] = soup.get_text(separator="\n")

        return message
