-- Additional-expense primary history is append-only, including raw SQL writes.
CREATE OR REPLACE FUNCTION procurement.reject_additional_expense_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'Additional expense source history is immutable';
END $$;
CREATE TRIGGER immutable_additional_expense_document
BEFORE UPDATE OR DELETE OR TRUNCATE ON procurement.additional_expense_document
FOR EACH STATEMENT EXECUTE FUNCTION procurement.reject_additional_expense_mutation();

CREATE OR REPLACE FUNCTION procurement.check_additional_expense(expense integer) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE h procurement.additional_expense_document%ROWTYPE;
        r procurement.additional_expense_revision%ROWTYPE;
        c accounting.source_control%ROWTYPE;
        n integer; latest integer;
BEGIN
  SELECT * INTO h FROM procurement.additional_expense_document WHERE id = expense;
  IF NOT FOUND THEN RAISE EXCEPTION 'Additional expense header is missing'; END IF;
  SELECT count(*), max(version) INTO n, latest FROM procurement.additional_expense_revision WHERE expense_id = expense;
  IF n = 0 OR latest <> n OR EXISTS (SELECT 1 FROM procurement.additional_expense_revision
      WHERE expense_id = expense AND version < 1) THEN
    RAISE EXCEPTION 'Additional expense revision chain is incomplete';
  END IF;
  SELECT * INTO r FROM procurement.additional_expense_revision WHERE expense_id = expense AND version = latest;
  SELECT * INTO c FROM accounting.source_control WHERE organization_id = h.organization_id
    AND source = 'procurement:additional-expense:' || expense;
  IF NOT FOUND OR c.version IS DISTINCT FROM latest OR c.month IS DISTINCT FROM left(r.document->>'operation_date', 7)
     OR (c.entry_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM accounting.late_cost_receipt l
       WHERE l.entry_id=c.entry_id AND l.organization_id=h.organization_id AND l.expense_id=expense
         AND l.source_version=latest)) THEN
    RAISE EXCEPTION 'Additional expense completeness registration is missing or inconsistent';
  END IF;
END $$;

CREATE OR REPLACE FUNCTION procurement.check_additional_expense_trigger() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_TABLE_NAME = 'additional_expense_document' THEN
    PERFORM procurement.check_additional_expense(NEW.id);
  ELSIF TG_TABLE_NAME = 'additional_expense_revision' THEN
    PERFORM procurement.check_additional_expense(NEW.expense_id);
  ELSE
    IF NEW.source LIKE 'procurement:additional-expense:%' THEN
      PERFORM procurement.check_additional_expense(substring(NEW.source FROM 32)::integer);
    END IF;
  END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER complete_additional_expense_header AFTER INSERT ON procurement.additional_expense_document
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION procurement.check_additional_expense_trigger();
CREATE CONSTRAINT TRIGGER complete_additional_expense_revision AFTER INSERT ON procurement.additional_expense_revision
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION procurement.check_additional_expense_trigger();
CREATE CONSTRAINT TRIGGER complete_additional_expense_control AFTER INSERT OR UPDATE ON accounting.source_control
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION procurement.check_additional_expense_trigger();

CREATE OR REPLACE FUNCTION procurement.guard_additional_expense_revision() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE owner_id integer; latest integer; link jsonb; saved jsonb; expected jsonb;
        pos integer := 0; source_document jsonb; posting procurement.receipt_posting%ROWTYPE;
        seen jsonb := '[]'::jsonb;
BEGIN
  SELECT organization_id INTO owner_id FROM procurement.additional_expense_document WHERE id = NEW.expense_id;
  PERFORM 1 FROM accounting.organization WHERE id = owner_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'Additional expense organization is missing'; END IF;
  SELECT coalesce(max(version), 0) INTO latest FROM procurement.additional_expense_revision WHERE expense_id = NEW.expense_id;
  IF NEW.version <> latest + 1 THEN RAISE EXCEPTION 'Additional expense must append the next version'; END IF;
  IF jsonb_typeof(NEW.document::jsonb->'receipt_lines') IS DISTINCT FROM 'array'
    OR jsonb_typeof(NEW.receipt_sources::jsonb) IS DISTINCT FROM 'array' THEN
    RAISE EXCEPTION 'Additional expense receipt links must be arrays';
  END IF;
  IF jsonb_array_length(NEW.document::jsonb->'receipt_lines') = 0
    OR jsonb_array_length(NEW.document::jsonb->'receipt_lines') <> jsonb_array_length(NEW.receipt_sources::jsonb) THEN
    RAISE EXCEPTION 'Additional expense receipt snapshots are incomplete';
  END IF;
  FOR link IN SELECT value FROM jsonb_array_elements(NEW.document::jsonb->'receipt_lines') LOOP
    IF link IS DISTINCT FROM jsonb_build_object('receipt_id', link->'receipt_id',
         'version', link->'version', 'line_number', link->'line_number')
      OR jsonb_typeof(link->'receipt_id') IS DISTINCT FROM 'number'
      OR jsonb_typeof(link->'version') IS DISTINCT FROM 'number'
      OR jsonb_typeof(link->'line_number') IS DISTINCT FROM 'number'
      OR link->>'receipt_id' !~ '^[1-9][0-9]*$' OR link->>'version' !~ '^[1-9][0-9]*$'
      OR link->>'line_number' !~ '^[1-9][0-9]*$' THEN
      RAISE EXCEPTION 'Additional expense receipt link must contain exact positive identifiers';
    END IF;
    IF seen @> jsonb_build_array(link) THEN RAISE EXCEPTION 'Additional expense receipt link is duplicated'; END IF;
    seen := seen || jsonb_build_array(link);
    SELECT p.* INTO posting FROM procurement.receipt_posting p JOIN procurement.receipt_document d ON d.id = p.receipt_id
      WHERE d.organization_id = owner_id AND p.receipt_id::text = link->>'receipt_id'
        AND p.version::text = link->>'version';
    IF NOT FOUND THEN RAISE EXCEPTION 'Additional expense receipt is not posted in this organization/version'; END IF;
    SELECT document::jsonb INTO source_document FROM procurement.receipt_revision
      WHERE receipt_id = posting.receipt_id AND version = posting.version;
    IF source_document IS NULL OR (link->>'line_number')::integer < 1
      OR (link->>'line_number')::integer > jsonb_array_length(source_document->'items') THEN
      RAISE EXCEPTION 'Additional expense receipt line is missing';
    END IF;
    expected := link || jsonb_build_object('entry_id', posting.entry_id, 'posting_digest', posting.digest,
      'document_date', source_document->'document_date', 'operation_date', source_document->'operation_date',
      'currency', source_document->'currency', 'warehouse', source_document->'warehouse',
      'item', source_document->'items'->((link->>'line_number')::integer - 1));
    saved := NEW.receipt_sources::jsonb->pos;
    IF saved IS DISTINCT FROM expected THEN RAISE EXCEPTION 'Additional expense receipt snapshot differs from source'; END IF;
    pos := pos + 1;
  END LOOP;
  RETURN NEW;
END $$;
CREATE TRIGGER guard_additional_expense_revision BEFORE INSERT ON procurement.additional_expense_revision
FOR EACH ROW EXECUTE FUNCTION procurement.guard_additional_expense_revision();

CREATE OR REPLACE FUNCTION procurement.guard_additional_expense_control_truncate() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM procurement.additional_expense_document) THEN
    RAISE EXCEPTION 'Additional expense completeness records cannot be truncated';
  END IF;
  RETURN NULL;
END $$;
CREATE TRIGGER preserve_additional_expense_completeness BEFORE TRUNCATE ON accounting.source_control
FOR EACH STATEMENT EXECUTE FUNCTION procurement.guard_additional_expense_control_truncate();
CREATE TRIGGER immutable_additional_expense_revision
BEFORE UPDATE OR DELETE OR TRUNCATE ON procurement.additional_expense_revision
FOR EACH STATEMENT EXECUTE FUNCTION procurement.reject_additional_expense_mutation();
