-- ghera's demo: two tenants, two applications, and the tables a real service has besides.
-- Run as the role the service connects as (the owner of every table), never a superuser.

CREATE TABLE invoice (            -- a tenant table with soft delete
    id         serial PRIMARY KEY,
    tenant_id  integer NOT NULL,
    amount     numeric NOT NULL,
    is_deleted boolean NOT NULL DEFAULT false
);
CREATE TABLE note (               -- a tenant table without it
    id        serial PRIMARY KEY,
    tenant_id integer NOT NULL,
    body      text NOT NULL
);
CREATE TABLE recipe (             -- owned by the application 'kitchen'
    id   serial PRIMARY KEY,
    name text NOT NULL
);
CREATE TABLE plot (               -- owned by 'garden', and declared open to 'kitchen'
    id   serial PRIMARY KEY,
    crop text NOT NULL
);
CREATE TABLE schema_version (     -- the service's own plumbing: known, never fenced
    version integer NOT NULL
);

INSERT INTO invoice (tenant_id, amount, is_deleted) VALUES (1, 10, false), (1, 11, false), (1, 12, true),
                                                         (2, 20, false), (2, 21, false), (2, 22, false);
INSERT INTO note (tenant_id, body) VALUES (1, 'one'), (2, 'two'), (2, 'three');
INSERT INTO recipe (name) VALUES ('dal'), ('rice');
INSERT INTO plot (crop) VALUES ('tomato'), ('chilli'), ('coriander');
INSERT INTO schema_version VALUES (1);
