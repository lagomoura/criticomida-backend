from pydantic import BaseModel


class OwnerNotificationPreferenceRead(BaseModel):
    notify_on_review: bool


class OwnerNotificationPreferenceUpdate(BaseModel):
    notify_on_review: bool
