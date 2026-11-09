-- Wissensraum-AI Server: eigene Datenbank und zwei Rollen (Release 3.0, Schritt 10.8).
-- Einmal als Superuser im vorhandenen PostgreSQL-18-Container ausführen, zum Beispiel:
--   docker exec -i <postgres-container> psql -U postgres -v owner_pw="$(cat secrets/db_owner.txt)" \
--     -v app_pw="$(cat secrets/db_app.txt)" < deploy/datenbank.sql
-- Die bestehende Datenbank der Desktop-Anwendung wird nicht angefasst.
CREATE ROLE wissensraum_owner LOGIN PASSWORD :'owner_pw';
CREATE ROLE wissensraum_app LOGIN PASSWORD :'app_pw';
CREATE DATABASE wissensraum_server OWNER wissensraum_owner ENCODING 'UTF8' TEMPLATE template0;
REVOKE ALL ON DATABASE wissensraum_server FROM PUBLIC;
GRANT CONNECT ON DATABASE wissensraum_server TO wissensraum_owner, wissensraum_app;
\connect wissensraum_server
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
-- Tabellen und Rechte der Laufzeitrolle legt danach "python server.py migrate" an (Eigentümerrolle).
