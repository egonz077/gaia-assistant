-- Gaia's users are real-estate developers, not brokers or agents.
--
-- 'agent' was the wrong word from the first commit. It describes a brokerage,
-- and it is the word the model reads in every system prompt — so it was
-- shaping the register of every reply, not just sitting in a column. Nothing
-- branches on this value today (no capability sets allowed_roles), so this is
-- a vocabulary correction rather than a permissions change.
--
-- 'developer' collides with the software sense of the word inside this
-- codebase, which is a little unfortunate; it is the term the industry uses
-- for these people, and the alternative ('member', 'principal') loses that.
ALTER TABLE users ALTER COLUMN role SET DEFAULT 'developer';
UPDATE users SET role = 'developer' WHERE role = 'agent';
