#!/usr/bin/env bash
# This file is sourced by the official MySQL docker-entrypoint.sh.
# Do NOT change the parent shell options here (especially `set -u`): doing so
# breaks the upstream entrypoint when it later reads optional unset variables.
if [[ ! "${SQL_SANDBOX_MARKER:-}" =~ ^[a-f0-9]{64}$ ]]; then
  echo 'Dedicated SQL sandbox marker is missing' >&2
  exit 1
fi
if ! MYSQL_PWD="${MYSQL_ROOT_PASSWORD:-}" mysql --protocol=socket --user=root <<SQL
CREATE DATABASE taskforge_sql_guard;
CREATE TABLE taskforge_sql_guard.runtime(singleton integer PRIMARY KEY CHECK(singleton=1), token varchar(64) NOT NULL);
INSERT INTO taskforge_sql_guard.runtime VALUES(1,'$SQL_SANDBOX_MARKER');
SQL
then
  echo 'Dedicated SQL sandbox guard initialization failed' >&2
  exit 1
fi
