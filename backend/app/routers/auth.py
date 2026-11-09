from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.app.database import get_db
from backend.app.limiter import limiter
from backend.app.models import Admin, AdminActivityLog
from backend.app.schemas import LoginRequest, PasswordChange, TokenOut
from backend.app.security import create_access_token, dummy_password_hash, get_current_admin, hash_password, verify_password


router = APIRouter(prefix="/api/auth", tags=["authentication"])


@router.post("/login", response_model=TokenOut)
@limiter.limit("10/minute")
def login(request: Request, payload: LoginRequest, db: Session = Depends(get_db)):
    admin = db.scalar(select(Admin).where(Admin.email == payload.email.lower()))
    valid = verify_password(payload.password, admin.password_hash if admin else dummy_password_hash)
    if admin is None or not admin.is_active or not valid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password.")
    token = create_access_token(admin)
    return {"access_token": token, "admin": admin}


@router.get("/me")
def me(admin: Admin = Depends(get_current_admin)):
    return {"id": admin.id, "name": admin.name, "email": admin.email}


@router.post("/logout", status_code=204)
def logout(admin: Admin = Depends(get_current_admin), db: Session = Depends(get_db)):
    # Revoke all current tokens for this administrator, including copies of the token.
    db.execute(update(Admin).where(Admin.id == admin.id).values(token_version=Admin.token_version + 1))
    db.add(AdminActivityLog(admin_id=admin.id, action="auth.logout", entity_type="admin", entity_id=str(admin.id)))
    db.commit()


@router.post("/password", status_code=204)
@limiter.limit("5/minute")
def change_password(request: Request, payload: PasswordChange,
                    admin: Admin = Depends(get_current_admin), db: Session = Depends(get_db)):
    locked = db.scalar(select(Admin).where(Admin.id == admin.id).with_for_update().execution_options(populate_existing=True))
    if not verify_password(payload.current_password, locked.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")
    locked.password_hash = hash_password(payload.new_password)
    locked.token_version += 1
    db.add(AdminActivityLog(admin_id=admin.id, action="auth.password_change", entity_type="admin", entity_id=str(admin.id)))
    db.commit()
