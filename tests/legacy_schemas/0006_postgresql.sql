-- The schema at migration 0006 (before organization isolation), generated from the models of that release.
-- Used by tests to prove that a database of that release upgrades in place. Do not edit.

CREATE TABLE do_not_call (
	id SERIAL NOT NULL, 
	phone_e164 VARCHAR(32) NOT NULL, 
	reason VARCHAR(200) NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);

CREATE UNIQUE INDEX ix_do_not_call_phone_e164 ON do_not_call (phone_e164);

CREATE TABLE jobs (
	id SERIAL NOT NULL, 
	kind VARCHAR(60) NOT NULL, 
	key VARCHAR(200) NOT NULL, 
	payload JSON NOT NULL, 
	status VARCHAR(12) NOT NULL, 
	attempts INTEGER NOT NULL, 
	max_attempts INTEGER NOT NULL, 
	timeout_seconds INTEGER NOT NULL, 
	run_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	locked_by VARCHAR(80), 
	locked_until TIMESTAMP WITHOUT TIME ZONE, 
	last_error TEXT, 
	call_run_id INTEGER, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	finished_at TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_jobs_call_run_id ON jobs (call_run_id);

CREATE INDEX ix_jobs_due ON jobs (status, run_at);

CREATE INDEX ix_jobs_kind ON jobs (kind);

CREATE UNIQUE INDEX uq_jobs_key ON jobs (key);

CREATE TABLE organizations (
	id SERIAL NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (name)
);

CREATE TABLE campaigns (
	id SERIAL NOT NULL, 
	organization_id INTEGER, 
	name VARCHAR(200) NOT NULL, 
	script_text TEXT NOT NULL, 
	playbook_json JSON NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	max_concurrent_calls INTEGER, 
	rules_json JSON NOT NULL, 
	stop_reason VARCHAR(300), 
	inbound_number VARCHAR(32), 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id), 
	UNIQUE (name)
);

CREATE INDEX ix_campaigns_inbound_number ON campaigns (inbound_number);

CREATE INDEX ix_campaigns_organization_id ON campaigns (organization_id);

CREATE UNIQUE INDEX uq_campaigns_inbound_number ON campaigns (inbound_number);

CREATE TABLE job_attempts (
	id SERIAL NOT NULL, 
	job_id INTEGER NOT NULL, 
	attempt INTEGER NOT NULL, 
	worker VARCHAR(80) NOT NULL, 
	outcome VARCHAR(12) NOT NULL, 
	error TEXT, 
	started_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	finished_at TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(job_id) REFERENCES jobs (id)
);

CREATE INDEX ix_job_attempts_job_id ON job_attempts (job_id);

CREATE TABLE users (
	id SERIAL NOT NULL, 
	organization_id INTEGER NOT NULL, 
	email VARCHAR(254) NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	role VARCHAR(12) NOT NULL, 
	password_hash VARCHAR(300) NOT NULL, 
	active BOOLEAN NOT NULL, 
	failed_logins INTEGER NOT NULL, 
	locked_until TIMESTAMP WITHOUT TIME ZONE, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	last_login_at TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id), 
	UNIQUE (email)
);

CREATE INDEX ix_users_organization_id ON users (organization_id);

CREATE TABLE website_requests (
	id SERIAL NOT NULL, 
	organization_id INTEGER NOT NULL, 
	kind VARCHAR(20) NOT NULL, 
	email VARCHAR(254), 
	name VARCHAR(120), 
	company VARCHAR(160), 
	phone VARCHAR(32), 
	team_size VARCHAR(16), 
	message TEXT, 
	status VARCHAR(12) NOT NULL, 
	handled_by VARCHAR(254), 
	handled_at TIMESTAMP WITHOUT TIME ZONE, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id)
);

CREATE INDEX ix_website_requests_kind ON website_requests (kind);

CREATE INDEX ix_website_requests_organization_id ON website_requests (organization_id);

CREATE INDEX ix_website_requests_status ON website_requests (status);

CREATE TABLE experiments (
	id SERIAL NOT NULL, 
	organization_id INTEGER NOT NULL, 
	campaign_id INTEGER NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	control_version INTEGER NOT NULL, 
	variant_version INTEGER NOT NULL, 
	variant_share FLOAT NOT NULL, 
	min_answered_per_arm INTEGER NOT NULL, 
	created_by VARCHAR(200) NOT NULL, 
	ended_by VARCHAR(200) NOT NULL, 
	started_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	ended_at TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id), 
	FOREIGN KEY(campaign_id) REFERENCES campaigns (id)
);

CREATE INDEX ix_experiments_campaign_id ON experiments (campaign_id);

CREATE INDEX ix_experiments_organization_id ON experiments (organization_id);

CREATE UNIQUE INDEX uq_experiments_one_running ON experiments (campaign_id) WHERE status = 'running';

CREATE TABLE leads (
	id SERIAL NOT NULL, 
	campaign_id INTEGER NOT NULL, 
	phone_e164 VARCHAR(32) NOT NULL, 
	raw_phone VARCHAR(64) NOT NULL, 
	first_name VARCHAR(120) NOT NULL, 
	last_name VARCHAR(120) NOT NULL, 
	company VARCHAR(200) NOT NULL, 
	email VARCHAR(200) NOT NULL, 
	timezone VARCHAR(64), 
	extra JSON NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	attempts INTEGER NOT NULL, 
	next_attempt_at TIMESTAMP WITHOUT TIME ZONE, 
	last_disposition VARCHAR(40), 
	priority INTEGER NOT NULL, 
	score INTEGER, 
	score_details JSON NOT NULL, 
	notes TEXT NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(campaign_id) REFERENCES campaigns (id)
);

CREATE INDEX ix_leads_campaign_id ON leads (campaign_id);

CREATE INDEX ix_leads_due ON leads (campaign_id, status, next_attempt_at);

CREATE INDEX ix_leads_phone_e164 ON leads (phone_e164);

CREATE INDEX ix_leads_status ON leads (status);

CREATE UNIQUE INDEX uq_leads_campaign_phone ON leads (campaign_id, phone_e164);

CREATE TABLE playbook_versions (
	id SERIAL NOT NULL, 
	organization_id INTEGER NOT NULL, 
	campaign_id INTEGER NOT NULL, 
	version INTEGER NOT NULL, 
	playbook_json JSON NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	source VARCHAR(30) NOT NULL, 
	note TEXT NOT NULL, 
	changes_json JSON NOT NULL, 
	created_by VARCHAR(200) NOT NULL, 
	approved_by VARCHAR(200) NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	activated_at TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id), 
	FOREIGN KEY(campaign_id) REFERENCES campaigns (id)
);

CREATE INDEX ix_playbook_versions_campaign_id ON playbook_versions (campaign_id);

CREATE INDEX ix_playbook_versions_organization_id ON playbook_versions (organization_id);

CREATE UNIQUE INDEX uq_playbook_versions_campaign_version ON playbook_versions (campaign_id, version);

CREATE UNIQUE INDEX uq_playbook_versions_one_active ON playbook_versions (campaign_id) WHERE status = 'active';

CREATE TABLE training_documents (
	id SERIAL NOT NULL, 
	organization_id INTEGER NOT NULL, 
	campaign_id INTEGER NOT NULL, 
	kind VARCHAR(10) NOT NULL, 
	filename VARCHAR(300) NOT NULL, 
	source_url VARCHAR(1000), 
	file_type VARCHAR(16) NOT NULL, 
	size_bytes INTEGER NOT NULL, 
	sha256 VARCHAR(64) NOT NULL, 
	storage_path VARCHAR(600), 
	version INTEGER NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	error TEXT, 
	language VARCHAR(16), 
	char_count INTEGER NOT NULL, 
	chunk_count INTEGER NOT NULL, 
	max_pages INTEGER NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	processed_at TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id), 
	FOREIGN KEY(campaign_id) REFERENCES campaigns (id)
);

CREATE INDEX ix_training_documents_campaign_id ON training_documents (campaign_id);

CREATE INDEX ix_training_documents_organization_id ON training_documents (organization_id);

CREATE INDEX ix_training_documents_status ON training_documents (status);

CREATE TABLE training_runs (
	id SERIAL NOT NULL, 
	organization_id INTEGER NOT NULL, 
	campaign_id INTEGER NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	error TEXT, 
	version INTEGER, 
	started_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	finished_at TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id), 
	FOREIGN KEY(campaign_id) REFERENCES campaigns (id)
);

CREATE INDEX ix_training_runs_campaign_id ON training_runs (campaign_id);

CREATE INDEX ix_training_runs_organization_id ON training_runs (organization_id);

CREATE TABLE user_sessions (
	id SERIAL NOT NULL, 
	user_id INTEGER NOT NULL, 
	token_hash VARCHAR(64) NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	expires_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	revoked_at TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id), 
	UNIQUE (token_hash)
);

CREATE INDEX ix_user_sessions_user_id ON user_sessions (user_id);

CREATE TABLE call_runs (
	id SERIAL NOT NULL, 
	lead_id INTEGER NOT NULL, 
	campaign_id INTEGER NOT NULL, 
	twilio_call_sid VARCHAR(64), 
	status VARCHAR(20) NOT NULL, 
	status_source VARCHAR(12), 
	answered_by VARCHAR(40), 
	mode VARCHAR(20) NOT NULL, 
	direction VARCHAR(10) NOT NULL, 
	experiment_id INTEGER, 
	experiment_arm VARCHAR(10), 
	playbook_version INTEGER, 
	cost_usd FLOAT, 
	language VARCHAR(16), 
	stream_connected BOOLEAN NOT NULL, 
	stream_token VARCHAR(64), 
	started_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	ended_at TIMESTAMP WITHOUT TIME ZONE, 
	duration_seconds INTEGER, 
	transcript JSON NOT NULL, 
	tool_events JSON NOT NULL, 
	summary JSON NOT NULL, 
	disposition VARCHAR(40), 
	recording_url VARCHAR(400), 
	error TEXT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(lead_id) REFERENCES leads (id), 
	FOREIGN KEY(campaign_id) REFERENCES campaigns (id)
);

CREATE INDEX ix_call_runs_campaign_id ON call_runs (campaign_id);

CREATE INDEX ix_call_runs_campaign_status ON call_runs (campaign_id, status);

CREATE INDEX ix_call_runs_experiment_id ON call_runs (experiment_id);

CREATE INDEX ix_call_runs_lead_id ON call_runs (lead_id);

CREATE INDEX ix_call_runs_lead_latest ON call_runs (lead_id, id);

CREATE INDEX ix_call_runs_twilio_call_sid ON call_runs (twilio_call_sid);

CREATE UNIQUE INDEX uq_call_runs_one_active_outbound_per_lead ON call_runs (lead_id) WHERE direction = 'outbound' AND status IN ('created', 'queued', 'initiated', 'ringing', 'in-progress');

CREATE UNIQUE INDEX uq_call_runs_twilio_call_sid ON call_runs (twilio_call_sid);

CREATE TABLE knowledge_chunks (
	id SERIAL NOT NULL, 
	organization_id INTEGER NOT NULL, 
	campaign_id INTEGER NOT NULL, 
	document_id INTEGER NOT NULL, 
	ordinal INTEGER NOT NULL, 
	source_label VARCHAR(400) NOT NULL, 
	text TEXT NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id), 
	FOREIGN KEY(campaign_id) REFERENCES campaigns (id), 
	FOREIGN KEY(document_id) REFERENCES training_documents (id)
);

CREATE INDEX ix_knowledge_chunks_campaign_id ON knowledge_chunks (campaign_id);

CREATE INDEX ix_knowledge_chunks_document_id ON knowledge_chunks (document_id);

CREATE INDEX ix_knowledge_chunks_organization_id ON knowledge_chunks (organization_id);

CREATE TABLE appointments (
	id SERIAL NOT NULL, 
	lead_id INTEGER NOT NULL, 
	call_run_id INTEGER, 
	starts_at VARCHAR(64) NOT NULL, 
	timezone VARCHAR(64) NOT NULL, 
	notes TEXT NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(lead_id) REFERENCES leads (id), 
	FOREIGN KEY(call_run_id) REFERENCES call_runs (id)
);

CREATE INDEX ix_appointments_lead_id ON appointments (lead_id);

CREATE UNIQUE INDEX uq_appointments_call_start ON appointments (call_run_id, starts_at);

CREATE TABLE usage_records (
	id SERIAL NOT NULL, 
	organization_id INTEGER, 
	campaign_id INTEGER NOT NULL, 
	call_run_id INTEGER NOT NULL, 
	kind VARCHAR(20) NOT NULL, 
	quantity_seconds INTEGER NOT NULL, 
	amount_usd NUMERIC(12, 4) NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id), 
	FOREIGN KEY(campaign_id) REFERENCES campaigns (id), 
	FOREIGN KEY(call_run_id) REFERENCES call_runs (id)
);

CREATE INDEX ix_usage_records_campaign_id ON usage_records (campaign_id);

CREATE INDEX ix_usage_records_organization_id ON usage_records (organization_id);

CREATE UNIQUE INDEX uq_usage_records_call_kind ON usage_records (call_run_id, kind);
