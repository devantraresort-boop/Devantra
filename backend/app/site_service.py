from backend.app.config import settings
from backend.app.models import SiteSettings


def site_info(db):
    record = db.get(SiteSettings, 1)
    if record:
        return {name: getattr(record, name) for name in ("name", "phone", "email", "address", "cancellation_policy")}
    return {"name": settings.resort_name, "phone": settings.resort_phone,
            "email": settings.admin_contact_email, "address": settings.resort_address,
            "cancellation_policy": ""}
