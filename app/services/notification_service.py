from sqlalchemy.orm import Session
from app.models.notification import Notification


def create_notification(
    db: Session,
    user_id: str,
    type: str,
    title: str,
    body: str | None = None,
    link: str | None = None,
):
    """Create a notification for a user. Silently no-ops if user_id is None."""
    if not user_id:
        return
    n = Notification(
        user_id=user_id,
        type=type,
        title=title,
        body=body,
        link=link,
    )
    db.add(n)
    # Do NOT commit here — caller commits as part of their transaction