-- Relational Fluency: analysis database schema (draft 1, 2026-09-22)
-- Dialect: PostgreSQL 15+. See docs/db-schema.md for scope, the load mapping
-- from the S3 archive, and the open questions.
--
-- Identifiers are the app's own string ids: participants p_<epoch>_<hex>,
-- encounters s_<epoch>_<hex>, runs 12 hex chars. Wall-clock times are
-- timestamptz; `t` columns are seconds from encounter start (numeric).
--
-- Checked: loads clean on postgres:16-alpine (docker run + psql -f).

BEGIN;

CREATE SCHEMA IF NOT EXISTS rf;
SET search_path TO rf, public;

-- ---------------------------------------------------------------------------
-- 1. Scenario bank (reference data, loaded from scenarios/v3/*.yaml)
-- ---------------------------------------------------------------------------

CREATE TABLE construct (
    construct           text PRIMARY KEY,           -- conflict_management, influence,
                                                    -- inspirational_leadership, teamwork
    label               text NOT NULL,              -- "Conflict Management"
    esci_cluster        text NOT NULL DEFAULT 'Relationship Management'
);

-- ESCI items are the same across a construct's parallel forms (checked S1A/S1B
-- .. S4A/S4B), so they key on the construct, not the scenario.
CREATE TABLE esci_item (
    construct           text NOT NULL REFERENCES construct,
    item_key            text NOT NULL,              -- key_people, de_escalate, fester_r ...
    label               text NOT NULL,              -- "Allows conflict to fester (R)"
    reverse_keyed       boolean NOT NULL DEFAULT false,   -- the *_r items
    PRIMARY KEY (construct, item_key)
);

CREATE TABLE scenario (
    scenario_id         text PRIMARY KEY,           -- S1A .. S4B
    construct           text NOT NULL REFERENCES construct,
    variant             char(1) NOT NULL CHECK (variant IN ('A','B','C')),
    parallel_form       text REFERENCES scenario,   -- S1A <-> S1B
    title               text NOT NULL,
    duration_min_minutes smallint NOT NULL,         -- duration_minutes: [7, 12]
    duration_max_minutes smallint NOT NULL,
    skill_measured      text,
    setup               text,                       -- participant-facing situation
    spec_sha256         text NOT NULL,              -- fingerprint of the YAML as fielded
    spec_version        text,                       -- git commit or tag of the bank
    fielded_from        date,
    fielded_to          date
);

CREATE TABLE scenario_agent (
    scenario_id         text NOT NULL REFERENCES scenario,
    agent_id            text NOT NULL,              -- riley, sam, morgan ...
    name                text NOT NULL,
    display_role        text,                       -- shown to the participant
    internal_role       text,                       -- behaviour policy (`role`); never shown
    voice_gemini_live   text,                       -- realtime_voice.gemini-live
    voice_gpt_realtime  text,                       -- realtime_voice.gpt-realtime
    system_prompt       text,                       -- the actor's brief, as fielded
    system_prompt_sha256 text,
    PRIMARY KEY (scenario_id, agent_id)
);

CREATE TABLE interaction (
    scenario_id         text NOT NULL REFERENCES scenario,
    interaction_id      text NOT NULL,              -- i1, i2
    position            smallint NOT NULL,
    mode                text NOT NULL CHECK (mode IN ('one_to_one','group','one_to_one_series')),
    kind                text,                       -- context_and_push, counterpart, opening,
                                                    -- group, individuals, planted_triggers, allocation
    label               text NOT NULL,
    opening             text,
    observe             text,                       -- what raters watch for
    agents              text[] NOT NULL,            -- agent_ids present ([agent] for one_to_one)
    PRIMARY KEY (scenario_id, interaction_id)
);

CREATE TABLE planted_trigger (
    scenario_id         text NOT NULL,
    interaction_id      text NOT NULL,
    trigger_id          text NOT NULL,              -- t1_retaliation_fork ...
    position            smallint NOT NULL,
    cue                 text NOT NULL,
    on_silence          text,
    esci_items          text[] NOT NULL,            -- item_keys of this scenario's construct
    high_answer         text,                       -- scores.high.answer / why
    high_why            text,
    low_answer          text,                       -- scores.low.answer / why
    low_why             text,
    PRIMARY KEY (scenario_id, trigger_id),
    FOREIGN KEY (scenario_id, interaction_id) REFERENCES interaction
);

-- ---------------------------------------------------------------------------
-- 2. People and runs
-- ---------------------------------------------------------------------------

-- Opaque participant record (data/participants/<pid>.json). Everything
-- analytical joins on participant_id. Consent is taken outside the platform.
CREATE TABLE participant (
    participant_id      text PRIMARY KEY,           -- p_<epoch>_<hex>
    cohort              text NOT NULL CHECK (cohort IN ('study','internal','unattributed')),
    created_at          timestamptz NOT NULL,
    minted_for_run_id   text,                       -- run_id on the record, if bound at mint
    withdrawn_at        timestamptz                 -- copied from the withdrawal stamp
);

-- Identity: the CloudResearch / Qualtrics keys. Restricted access; joins only.
CREATE TABLE participant_identity (
    participant_id      text PRIMARY KEY REFERENCES participant,
    participant_key     text NOT NULL,              -- `code`: CloudResearch Connect participantId
    participant_key_status text,                    -- "ok" or the normalisation failure
    raw_participant_key text                        -- what actually arrived on the link
);

CREATE INDEX participant_identity_key_idx ON participant_identity (participant_key);

CREATE TABLE run (
    run_id              text PRIMARY KEY,           -- 12 hex
    participant_id      text REFERENCES participant,
    cohort              text NOT NULL,
    created_at          timestamptz NOT NULL,
    arm                 text NOT NULL DEFAULT 'full',   -- construct_pool.arm
    construct_pool      jsonb,                      -- verbatim: which constructs were allowed
    order_scheme        text NOT NULL,              -- order.scheme: williams_4x4 | shuffle
    order_row           smallint,                   -- order.row (NULL under shuffle)
    form_exclusions     jsonb,                      -- forms steered away from, verbatim
    variants            jsonb,                      -- per-slot form pins, verbatim
    survey1_response_id text,                       -- qualtrics_id: Survey 1 ResponseID
    survey2_response_id text,                       -- from the Survey 2 export, when joined
    completion_code     text,                       -- RF-XXXXXXXX (HMAC of the run; derived at load)
    completed_at        timestamptz,                -- when the 4th encounter closed
    attempt_number      smallint NOT NULL DEFAULT 1, -- RCT: 1 or 2
    sibling_run_id      text REFERENCES run,        -- RCT: the other attempt
    other_arm_runs      jsonb                       -- cross-links to this participant's runs in other arms
);

CREATE INDEX run_participant_idx ON run (participant_id);
CREATE INDEX run_survey1_idx ON run (survey1_response_id);

-- The four assignments of a run, whether or not the encounter happened.
CREATE TABLE run_slot (
    run_id              text NOT NULL REFERENCES run,
    slot                smallint NOT NULL CHECK (slot BETWEEN 0 AND 3),
    construct           text NOT NULL REFERENCES construct,
    scenario_id         text NOT NULL REFERENCES scenario,
    encounter_id        text,                       -- filled when the encounter closes
    PRIMARY KEY (run_id, slot)
);

-- The stop button. One row per run that carries a stamp; the run they actually
-- pressed stop in has withdrawn_on_run_id = run_id, copies point at it.
CREATE TABLE withdrawal (
    run_id              text PRIMARY KEY REFERENCES run,
    withdrawn_at        timestamptz NOT NULL,       -- stamp.at
    reason              text NOT NULL,              -- participant_withdrew, ...
    encounter_id        text,                       -- stamp.session_id: where they were
    slot_index          smallint,                   -- stamp.index: how far the run had got
    completed_count     smallint,                   -- stamp.completed
    withdrawn_on_run_id text NOT NULL REFERENCES run
);

-- ---------------------------------------------------------------------------
-- 3. Encounters (one recorded conversation) and what happened in them
-- ---------------------------------------------------------------------------

CREATE TABLE encounter (
    encounter_id        text PRIMARY KEY,           -- s_<epoch>_<hex>
    run_id              text REFERENCES run,        -- NULL for direct /v2?scenario= links
    slot                smallint,                   -- encounter_index within the run
    participant_id      text REFERENCES participant,
    scenario_id         text NOT NULL REFERENCES scenario,
    cohort              text NOT NULL,
    started_at          timestamptz NOT NULL,
    ended_at            timestamptz,
    duration_s          numeric(8,1),
    status              text NOT NULL,              -- closed, abandoned, ...
    -- provenance: what produced the data
    gateway             text NOT NULL,
    realtime_model      text NOT NULL,
    text_model          text,
    director_model      text,
    deploy_revision     int,                        -- ECS task-definition revision, if known
    spec_sha256         text,                       -- spec_fingerprint.sha256 at the time
    spec_trigger_ids    text[],                     -- spec_fingerprint.trigger_ids
    -- integrity (record.json counts / participant_channel / video_upload)
    participant_turns   int NOT NULL DEFAULT 0,
    agent_turns         int NOT NULL DEFAULT 0,
    stage_directions    int NOT NULL DEFAULT 0,
    script_mismatch_turns int NOT NULL DEFAULT 0,
    unheard_turns       int NOT NULL DEFAULT 0,
    participant_channel_state text,                 -- ok | lost | restored
    participant_channel_losses int DEFAULT 0,
    untranscribed_s     numeric(8,1),
    video_upload_state  text,                       -- uploaded | failed | none
    video_upload_attempts int DEFAULT 0,
    video_upload_error  text,
    archive_uri         text,                       -- s3://.../encounters/<id>/
    FOREIGN KEY (run_id, slot) REFERENCES run_slot (run_id, slot) DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX encounter_run_idx ON encounter (run_id);
CREATE INDEX encounter_participant_idx ON encounter (participant_id);
CREATE INDEX encounter_scenario_idx ON encounter (scenario_id, started_at);

-- The cast as fielded (voice differs from the authored one by model family).
CREATE TABLE encounter_cast (
    encounter_id        text NOT NULL REFERENCES encounter,
    agent_id            text NOT NULL,
    name                text NOT NULL,
    voice               text,                       -- voice actually used
    PRIMARY KEY (encounter_id, agent_id)
);

-- One row per spoken turn, both sides, in order (record.json transcript[]).
CREATE TABLE turn (
    turn_id             bigserial PRIMARY KEY,
    encounter_id        text NOT NULL REFERENCES encounter,
    seq                 int NOT NULL,               -- 0..n within the encounter
    t                   numeric(9,3) NOT NULL,      -- seconds from encounter start
    wall                timestamptz,
    role                text NOT NULL CHECK (role IN ('participant','agent')),
    agent_id            text,                       -- NULL for the participant
    segment             smallint,
    interaction_id      text,
    text                text,                       -- live transcript
    interrupted         boolean NOT NULL DEFAULT false,
    transcript_missing  boolean NOT NULL DEFAULT false,
    garbled             boolean NOT NULL DEFAULT false,
    script_mismatch     boolean NOT NULL DEFAULT false,  -- caption came back in another script
    participant_channel text,                       -- which session transcribed the participant
    audio_ms            int,                        -- agent turns: audio actually played
    latency_s           numeric(6,2),               -- participant end -> agent first audio
    UNIQUE (encounter_id, seq)
);

-- What the director told each actor, paired with the reply it shaped
-- (record.json steering_log[]).
CREATE TABLE stage_direction (
    direction_id        bigserial PRIMARY KEY,
    encounter_id        text NOT NULL REFERENCES encounter,
    turn_index          int,                        -- director's turn counter
    reply_turn_id       bigint REFERENCES turn,     -- the agent turn it produced, if any
    t                   numeric(9,3) NOT NULL,
    agent_id            text NOT NULL,
    segment             smallint,
    interaction_id      text,
    direction           text,                       -- NULL = unsteered turn (distinct from lost)
    trigger_id          text,                       -- planted trigger being fired, if any
    esci_items          text[],
    probing             boolean NOT NULL DEFAULT false,  -- on_silence probe
    opening             boolean NOT NULL DEFAULT false,
    via                 text,                       -- how it reached the actor
    acked               boolean,
    director_model      text,
    instructions_sha256 text
);

CREATE INDEX stage_direction_encounter_idx ON stage_direction (encounter_id);

-- Planted triggers as they actually fired (the measurement). trigger_fired is
-- append-only in events.jsonl; a later trigger_undelivered with the same
-- `index` cancels it, so the loader nets the two into one row per beat.
CREATE TABLE trigger_firing (
    firing_id           bigserial PRIMARY KEY,
    encounter_id        text NOT NULL REFERENCES encounter,
    trigger_id          text NOT NULL,
    interaction_id      text,
    plan_index          smallint,                   -- `index` in the trigger plan
    t                   numeric(9,3) NOT NULL,
    agent_id            text,
    probing             boolean NOT NULL DEFAULT false,
    esci_items          text[],
    outcome             text NOT NULL CHECK (outcome IN ('fired','undelivered')),
    undelivered_reason  text,                       -- floor_grant_failed, ...
    UNIQUE (encounter_id, trigger_id, plan_index, t)
);

-- Who the director chose to speak on each participant turn (rooms).
CREATE TABLE director_route (
    encounter_id        text NOT NULL REFERENCES encounter,
    t                   numeric(9,3) NOT NULL,
    speakers            text[] NOT NULL,
    addressed           text,                       -- agent named by the participant
    fallback            boolean NOT NULL DEFAULT false,
    fallback_reason     text,
    fallback_detail     text,
    PRIMARY KEY (encounter_id, t)
);

-- The raw event trail (events.jsonl), for anything the typed tables do not
-- carry: playback_cut, deferral_output, held_reply_adopted, voice_error ...
CREATE TABLE event (
    event_id            bigserial PRIMARY KEY,
    encounter_id        text NOT NULL REFERENCES encounter,
    seq                 int NOT NULL,               -- line number in events.jsonl
    t                   numeric(9,3),
    wall                timestamptz,
    type                text NOT NULL,
    agent_id            text,
    payload             jsonb NOT NULL,
    UNIQUE (encounter_id, seq)
);

CREATE INDEX event_encounter_type_idx ON event (encounter_id, type);

-- ---------------------------------------------------------------------------
-- 4. Media and alternative transcripts (files stay in S3; rows point at them)
-- ---------------------------------------------------------------------------

CREATE TABLE media (
    media_id            bigserial PRIMARY KEY,
    encounter_id        text NOT NULL REFERENCES encounter,
    kind                text NOT NULL CHECK (kind IN ('participant_audio','agent_audio','webcam_video')),
    agent_id            text,                       -- agent_audio only
    s3_key              text NOT NULL UNIQUE,
    bytes               bigint,
    duration_s          numeric(8,1),
    sample_rate         int,
    channels            smallint,
    format              text,                       -- wav, webm
    uploaded_at         timestamptz
);

CREATE TABLE transcript_version (
    transcript_id       bigserial PRIMARY KEY,
    encounter_id        text NOT NULL REFERENCES encounter,
    source              text NOT NULL CHECK (source IN ('live','retranscribed','recovered_from_video')),
    scope               text NOT NULL CHECK (scope IN ('participant','dialogue')),
    model               text,                       -- transcription model used
    text                text NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (encounter_id, source, scope)
);

-- ---------------------------------------------------------------------------
-- 5. Surveys (Qualtrics), joined by ResponseID
-- ---------------------------------------------------------------------------

CREATE TABLE survey (
    survey_id           text PRIMARY KEY,           -- SV_...
    role                text NOT NULL CHECK (role IN ('survey1','survey2')),
    title               text
);

CREATE TABLE survey_response (
    response_id         text PRIMARY KEY,           -- R_...
    survey_id           text NOT NULL REFERENCES survey,
    participant_id      text REFERENCES participant, -- resolved via the run when present
    participant_key     text,                       -- as piped into the survey
    run_id              text REFERENCES run,        -- survey2 carries run + code
    completion_code     text,
    started_at          timestamptz,
    recorded_at         timestamptz,
    finished            boolean,
    duration_s          int,
    embedded            jsonb,                      -- embedded-data fields verbatim
    answers             jsonb                       -- question id -> value, verbatim
);

CREATE INDEX survey_response_participant_idx ON survey_response (participant_id);

-- ---------------------------------------------------------------------------
-- 6. Ratings (Phase 2 humans) and scores (Phase 3 model)
-- ---------------------------------------------------------------------------

CREATE TABLE rater (
    rater_id            text PRIMARY KEY,
    display_name        text,
    trained_at          date,
    active              boolean NOT NULL DEFAULT true
);

CREATE TABLE rating_assignment (
    assignment_id       bigserial PRIMARY KEY,
    encounter_id        text NOT NULL REFERENCES encounter,
    rater_id            text NOT NULL REFERENCES rater,
    assigned_at         timestamptz NOT NULL DEFAULT now(),
    due_at              date,
    status              text NOT NULL DEFAULT 'assigned'
                        CHECK (status IN ('assigned','in_progress','done','withdrawn')),
    UNIQUE (encounter_id, rater_id)
);

CREATE TABLE rating (
    rating_id           bigserial PRIMARY KEY,
    assignment_id       bigint NOT NULL REFERENCES rating_assignment,
    construct           text NOT NULL,
    item_key            text NOT NULL,
    score               smallint CHECK (score BETWEEN 1 AND 5),
    evidence            text,                       -- quote or note
    evidence_turn_ids   bigint[],                   -- turns the rater pointed at
    rated_at            timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (construct, item_key) REFERENCES esci_item,
    UNIQUE (assignment_id, construct, item_key)
);

CREATE TABLE model_score (
    score_id            bigserial PRIMARY KEY,
    encounter_id        text NOT NULL REFERENCES encounter,
    scorer_model        text NOT NULL,
    scorer_version      text NOT NULL,              -- prompt/rubric version or git sha
    construct           text NOT NULL,
    item_key            text NOT NULL,
    score               numeric(4,2),
    evidence            jsonb,
    scored_at           timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (construct, item_key) REFERENCES esci_item,
    UNIQUE (encounter_id, scorer_model, scorer_version, construct, item_key)
);

-- ---------------------------------------------------------------------------
-- 7. RCT (Phase 4)
-- ---------------------------------------------------------------------------

CREATE TABLE rct_assignment (
    participant_id      text PRIMARY KEY REFERENCES participant,
    arm                 text NOT NULL,              -- self_reflection | feedback_base | feedback_finetuned
    assigned_at         timestamptz NOT NULL,
    seed                text
);

CREATE TABLE feedback_delivery (
    delivery_id         bigserial PRIMARY KEY,
    run_id              text NOT NULL REFERENCES run,
    kind                text NOT NULL,              -- self_reflection_prompt | model_feedback
    content_uri         text,                       -- S3 or inline
    content             jsonb,
    delivered_at        timestamptz NOT NULL,
    viewed_s            int
);

-- ---------------------------------------------------------------------------
-- 8. Operations: what was running when
-- ---------------------------------------------------------------------------

CREATE TABLE deployment (
    revision            int PRIMARY KEY,            -- ECS task-definition revision
    image_tag           text NOT NULL,              -- git short sha
    realtime_model      text NOT NULL,
    director_model      text,
    applied_at          timestamptz NOT NULL,
    applied_by          text
);

-- ---------------------------------------------------------------------------
-- 9. Views the analysis will actually query
-- ---------------------------------------------------------------------------

CREATE VIEW v_encounter_summary AS
SELECT e.encounter_id, e.run_id, e.slot, e.participant_id, e.cohort,
       e.scenario_id, s.construct, s.variant,
       e.started_at, e.duration_s, e.realtime_model, e.director_model,
       e.participant_turns, e.agent_turns, e.stage_directions,
       (SELECT count(*) FROM trigger_firing f
         WHERE f.encounter_id = e.encounter_id AND f.outcome = 'fired') AS triggers_fired,
       (SELECT count(*) FROM planted_trigger p
         WHERE p.scenario_id = e.scenario_id) AS triggers_planned,
       EXISTS (SELECT 1 FROM media m
                WHERE m.encounter_id = e.encounter_id AND m.kind = 'webcam_video') AS has_video,
       e.participant_channel_losses, e.script_mismatch_turns
FROM encounter e
JOIN scenario s USING (scenario_id);

CREATE VIEW v_participant_progress AS
SELECT r.run_id, r.participant_id, r.cohort, r.created_at, r.arm,
       count(e.encounter_id) AS encounters_done,
       r.completed_at IS NOT NULL AS completed,
       r.survey1_response_id IS NOT NULL AS has_survey1,
       r.survey2_response_id IS NOT NULL AS has_survey2,
       w.withdrawn_at
FROM run r
LEFT JOIN encounter e ON e.run_id = r.run_id AND e.status = 'closed'
LEFT JOIN withdrawal w ON w.run_id = r.run_id
GROUP BY r.run_id, w.withdrawn_at;

CREATE VIEW v_esci_coverage AS
SELECT e.encounter_id, e.scenario_id, s.construct,
       array_agg(DISTINCT item) FILTER (WHERE item IS NOT NULL) AS items_exercised,
       (SELECT count(*) FROM esci_item i WHERE i.construct = s.construct) AS items_in_construct
FROM encounter e
JOIN scenario s USING (scenario_id)
LEFT JOIN stage_direction d ON d.encounter_id = e.encounter_id
LEFT JOIN LATERAL unnest(d.esci_items) AS item ON true
GROUP BY e.encounter_id, e.scenario_id, s.construct;

COMMIT;
