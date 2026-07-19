from pydantic import BaseModel, EmailStr
from typing import Optional, List

class UserSignup(BaseModel):
    username: str
    email: EmailStr
    password: str
    agreed_terms: Optional[bool] = None

class UserVerify(BaseModel):
    username: str
    otp: str

class UserLogin(BaseModel):
    username: str
    password: str

class ResendOTP(BaseModel):
    username: str

class ForgotPasswordRequest(BaseModel):
    email: EmailStr

class VerifyResetOTP(BaseModel):
    email: EmailStr
    otp: str

class ResetPassword(BaseModel):
    email: EmailStr
    otp: str
    new_password: str

class LinkItem(BaseModel):
    label: str
    url: str

class ProfileUpdate(BaseModel):
    phone: Optional[str] = None
    custom_code: Optional[str] = None
    links: Optional[List[LinkItem]] = None

class SessionRevoke(BaseModel):
    session_id: int

class AccountDelete(BaseModel):
    password: str

class TwoFactorSetup(BaseModel):
    enable: bool

class TwoFactorVerify(BaseModel):
    code: str
    temp_token: Optional[str] = None

class TwoFactorConfirm(BaseModel):
    password: str
    code: str

class ChangePassword(BaseModel):
    current_password: str
    new_password: str
    totp_code: Optional[str] = None

class AvailabilityCheck(BaseModel):
    username: Optional[str] = None
    email: Optional[str] = None

class ExecuteCodeRequest(BaseModel):
    language: str
    code: str
    stdin: Optional[str] = None

class JobCreateRequest(BaseModel):
    name: str
    language: str
    code: str
    env_vars: Optional[dict] = None

class JobAccessToggle(BaseModel):
    public: bool = True

class JobUpdateRequest(BaseModel):
    code: str

class UserPreferencesUpdate(BaseModel):
    theme: Optional[str] = None
    language: Optional[str] = None
    timezone: Optional[str] = None
    notifications_enabled: Optional[bool] = None
    email_notifications: Optional[bool] = None

class SnippetCreate(BaseModel):
    title: str
    language: Optional[str] = "text"
    content: str

class SnippetUpdate(BaseModel):
    id: int
    title: Optional[str] = None
    language: Optional[str] = None
    content: Optional[str] = None

class GenericDelete(BaseModel):
    id: int

class SnippetShare(BaseModel):
    id: int
    share: bool = True

class AdminSuspend(BaseModel):
    user_id: int
    suspended: bool
    code: Optional[str] = None

class AbuseReportIn(BaseModel):
    url: str
    reason: Optional[str] = ""

class ActivityLogEntry(BaseModel):
    action: str
    details: str = ""
