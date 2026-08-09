
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

    def update_status(Self,text, media , msg):
        post=msg.post

        Self.logger.debug(f"REPLYING TO: {post}")

        this_parent = models.create_strong_ref(post)
        this_root = models.create_strong_ref(post)

        with open(media, 'rb') as video:
            vid_data = video.read()

        if len(vid_data) == 0:
            raise ValueError(f"Video data is empty for file: {media}")

        Self.logger.debug(f"Uploading video {media} ({len(vid_data)} bytes)")
        status= Self.api.send_video(
            text        =   text,
            reply_to    =   models.AppBskyFeedPost.ReplyRef(parent=this_parent, root=this_root),
            video       =   vid_data
            )

        resp = Self.api.app.bsky.feed.get_post_thread(
            params=models.AppBskyFeedGetPostThread.Params(
                uri=status.uri
            )
        )
        Self.logger.info(f"Response: {resp.thread.post.record.embed}")
        return status

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
