export interface AccessUser {
  id: string;
  common_name: string | null;
  subject: string;
  issuer: string;
  serial_number: string;
  fingerprint: string;
  not_before: string;
  not_after: string;
  created_at: string;
  last_seen_at: string | null;
  status: "active" | "revoked";
  revoked_at: string | null;
  revocation_reason: string | null;
}

export interface AccessTokenRecord {
  id: string;
  user_id: string;
  common_name?: string | null;
  prefix: string;
  created_at: string;
  expires_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
  status: "active" | "revoked" | "expired";
}

export interface AccessAdmin {
  id: string;
  username: string;
  status: "active" | "disabled";
  created_at: string;
  last_login_at: string | null;
}

export interface AccessEvent {
  id: string;
  created_at: string;
  actor: string;
  action: string;
  target_type: string;
  target_id: string;
  address: string;
  details: string;
}

export interface AccessPage<T> {
  items: T[];
  total: number;
}

export interface AccessAdminSession {
  admin: AccessAdmin;
  csrf_token: string;
}

export interface BrowserAccessStatus {
  enabled: boolean;
  authenticated: boolean;
  certificate_present: boolean;
  certificate_mode?: "presented" | "trusted_ca";
  user: AccessUser | null;
}
