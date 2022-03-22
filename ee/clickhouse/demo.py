from typing import Dict, List
from uuid import uuid4

from posthog.models.event.util import create_event
from posthog.models.session_recording_event.util import create_session_recording_event


def bulk_create_events(events: List[Dict], **kw):
    for event_data in events:
        create_event(**event_data, **kw, event_uuid=uuid4())


def bulk_create_session_recording_events(events: List[Dict], **kw):
    for data in events:
        create_session_recording_event(**data, **kw, uuid=uuid4())
