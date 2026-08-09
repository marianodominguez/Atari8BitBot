
from typing import Self

from atproto import Client, client_utils
from atproto_client import models
from bs4 import BeautifulSoup as bs
import logging
import re
import os
import datetime
from zoneinfo import ZoneInfo

from types import SimpleNamespace

from requests import session

class BlueSkyApi:
    logger = logging.getLogger(__name__)
    api = None

    def get_api(Self, client_id, client_secret, access_token, access_token_secret):
        c=Client()
        c.login(client_id, client_secret)
        Self.api=c

        session = Self.api.com.atproto.server.get_session()
        Self.logger.info(f"Session: {session.email_confirmed}")

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
        """Derive the current PDS's DID from the active session's service endpoint."""
        host = Self.api._session.data.pds_endpoint  # e.g. 'https://blusher.us-east.host.bsky.network'
        host = host.split('://')[1].rstrip('/')
        return f"did:web:{host}"

    def update_status(Self, text, media, msg):
        post = msg.post
        this_parent = models.create_strong_ref(post)
        this_root = models.create_strong_ref(post)

        with open(media, 'rb') as f:
            vid_data = f.read()

        pds_did = Self._get_pds_did()

        # 1. Service auth token audience = your own PDS, not video.bsky.app
        token = Self.api.com.atproto.server.get_service_auth(
            params=models.ComAtprotoServerGetServiceAuth.Params(
                aud=pds_did,
                lxm='app.bsky.video.uploadVideo',
                exp=int(datetime.datetime.now(tz=ZoneInfo("UTC")).timestamp()) + 60 * 30,
            )
        ).token

        # 2. Upload still goes to the video service host itself
        video_client = Client('https://video.bsky.app')
        video_client._set_auth_headers(token)  # or: request(..., headers={'Authorization': f'Bearer {token}'})

        job = video_client.app.bsky.video.upload_video(data=vid_data)
        Self.logger.debug(f"Video job started: {job.job_status.job_id}, state={job.job_status.state}")

        # 3. Poll for completion — get_job_status is a public unauthenticated call
        import time
        status = job.job_status
        while status.state not in ('JOB_STATE_COMPLETED', 'JOB_STATE_FAILED'):
            time.sleep(2)
            status = Self.api.app.bsky.video.get_job_status(
                params=models.AppBskyVideoGetJobStatus.Params(job_id=status.job_id)
            ).job_status
            Self.logger.debug(f"Job status: {status.state} progress={status.progress}")

        if status.state == 'JOB_STATE_FAILED':
            raise RuntimeError(f"Video transcoding failed: {status.error}")

        # 4. Post with the fully-processed blob
        embed = models.AppBskyEmbedVideo.Main(video=status.blob, alt='')
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
            #parse the message to extract entities
            message=Self.extract_entities(post.record.text)
            status=SimpleNamespace()
            status.post=post
            ts=datetime.datetime.fromisoformat(post.record.created_at)
            #offset 100 milliseconds to avoid getting the same message
            status.id = int( ts.timestamp()*1000 + 100)
            #status.id=post.cid
            status.entities={}
            if 'urls' in message.keys():
                if 'urls' not in status.entities.keys():
                    status.entities['urls']=[]
                status.entities['urls']=message['urls']
            status.user=SimpleNamespace()
            status.user.screen_name=post.author.display_name
            status.user.name=post.author.handle
            status.full_text=message['text'].strip()
            replies[status.id]=status
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
