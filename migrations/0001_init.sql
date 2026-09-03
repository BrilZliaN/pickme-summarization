CREATE TABLE IF NOT EXISTS chats (
  chat_id                 INTEGER PRIMARY KEY,
  title                   TEXT,
  created_at              INTEGER NOT NULL,
  rolling_summary         TEXT,
  rolling_summary_at      INTEGER,
  message_count           INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS users (
  user_id        INTEGER PRIMARY KEY,
  username       TEXT,
  display_name   TEXT,
  first_seen_at  INTEGER NOT NULL,
  last_seen_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id             INTEGER NOT NULL REFERENCES chats(chat_id),
  user_id             INTEGER REFERENCES users(user_id),
  text                TEXT,
  reply_to_message_id INTEGER,
  media_type          TEXT,
  media_meta          TEXT,
  created_at          INTEGER NOT NULL,
  is_bot              INTEGER NOT NULL DEFAULT 0,
  is_edited           INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_chat_time ON messages(chat_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(chat_id, user_id);

CREATE TABLE IF NOT EXISTS user_profiles (
  chat_id          INTEGER NOT NULL,
  user_id          INTEGER NOT NULL,
  version          INTEGER NOT NULL DEFAULT 1,
  activity_score   REAL    NOT NULL DEFAULT 0,
  msg_count        INTEGER NOT NULL DEFAULT 0,
  since_message_id INTEGER NOT NULL DEFAULT 0,
  last_updated_at  INTEGER NOT NULL,
  profile_json     TEXT    NOT NULL,
  PRIMARY KEY (chat_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_profiles_activity ON user_profiles(chat_id, activity_score DESC);

CREATE TABLE IF NOT EXISTS memory_jobs (
  chat_id          INTEGER NOT NULL,
  user_id          INTEGER NOT NULL,
  since_message_id INTEGER NOT NULL,
  status           TEXT NOT NULL DEFAULT 'pending',
  created_at       INTEGER NOT NULL,
  PRIMARY KEY (chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS llm_log (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  feature          TEXT,
  provider         TEXT,
  model            TEXT,
  prompt_tokens    INTEGER,
  completion_tokens INTEGER,
  created_at       INTEGER NOT NULL,
  ok               INTEGER,
  error            TEXT
);
