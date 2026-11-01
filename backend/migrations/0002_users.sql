CREATE TABLE users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    username text NOT NULL UNIQUE,
    password_hash text NOT NULL,
    role text NOT NULL CHECK (role IN (
        'reporter',
        'triager',
        'investigator',
        'reviewer',
        'disclosure_approver',
        'auditor',
        'admin'
    )),
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);
