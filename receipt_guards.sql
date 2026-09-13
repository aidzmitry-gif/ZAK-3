-- Source document history, independent of the accounting ledger's tables.
CREATE OR REPLACE FUNCTION procurement.reject_receipt_revision_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'Source revisions are immutable';
END $$;
CREATE TRIGGER immutable_receipt_revision BEFORE UPDATE OR DELETE ON procurement.receipt_revision
FOR EACH ROW EXECUTE FUNCTION procurement.reject_receipt_revision_mutation();
CREATE TRIGGER immutable_receipt_document_delete BEFORE DELETE ON procurement.receipt_document
FOR EACH ROW EXECUTE FUNCTION procurement.reject_receipt_revision_mutation();

CREATE OR REPLACE FUNCTION procurement.guard_receipt_document() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.current_version <> 1 OR NEW.status <> 'draft' THEN
      RAISE EXCEPTION 'New receipt must start as draft version 1';
    END IF;
  ELSE
    IF NEW.organization_id IS DISTINCT FROM OLD.organization_id
       OR NEW.source_key IS DISTINCT FROM OLD.source_key
       OR NEW.created_by IS DISTINCT FROM OLD.created_by
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR OLD.status <> 'draft' OR NEW.status <> 'draft'
       OR EXISTS (SELECT 1 FROM procurement.receipt_posting WHERE receipt_id = OLD.id)
       OR NEW.current_version <> OLD.current_version + 1 THEN
      RAISE EXCEPTION 'Receipt identity is immutable; only append the next draft version';
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER guard_receipt_document BEFORE INSERT OR UPDATE ON procurement.receipt_document
FOR EACH ROW EXECUTE FUNCTION procurement.guard_receipt_document();

CREATE OR REPLACE FUNCTION procurement.check_receipt_revision_chain() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE revision_count integer; last_version integer;
BEGIN
  SELECT count(*), max(version) INTO revision_count, last_version
    FROM procurement.receipt_revision WHERE receipt_id = NEW.id;
  IF revision_count <> NEW.current_version OR last_version IS DISTINCT FROM NEW.current_version THEN
    RAISE EXCEPTION 'Receipt revision chain is incomplete';
  END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER complete_receipt_revision AFTER INSERT OR UPDATE ON procurement.receipt_document
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION procurement.check_receipt_revision_chain();

CREATE OR REPLACE FUNCTION procurement.guard_receipt_revision_insert() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE expected integer; owner_id integer;
BEGIN
  SELECT current_version, organization_id INTO expected, owner_id FROM procurement.receipt_document WHERE id = NEW.receipt_id FOR UPDATE;
  IF NEW.version IS DISTINCT FROM expected OR NEW.version < 1 THEN
    RAISE EXCEPTION 'Receipt revision does not match its document';
  END IF;
  IF EXISTS (SELECT 1 FROM json_array_elements(NEW.document->'items') item
    WHERE item->>'order_id' IS NOT NULL AND NOT EXISTS (
      SELECT 1 FROM procurement.purchase_ownership o WHERE o.organization_id = owner_id
        AND o.kind = 'order' AND o.source_id::text = item->>'order_id'
    )) THEN
    RAISE EXCEPTION 'Receipt orders must belong to its organization';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER guard_receipt_revision_insert BEFORE INSERT ON procurement.receipt_revision
FOR EACH ROW EXECUTE FUNCTION procurement.guard_receipt_revision_insert();

CREATE TRIGGER immutable_receipt_posting BEFORE UPDATE OR DELETE ON procurement.receipt_posting
FOR EACH ROW EXECUTE FUNCTION procurement.reject_receipt_revision_mutation();

CREATE OR REPLACE FUNCTION procurement.guard_receipt_posting_insert() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE expected integer;
BEGIN
  SELECT current_version INTO expected FROM procurement.receipt_document WHERE id = NEW.receipt_id FOR UPDATE;
  IF NEW.version IS DISTINCT FROM expected OR NEW.version < 1 THEN
    RAISE EXCEPTION 'Posting must reference the current source revision';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER guard_receipt_posting_insert BEFORE INSERT ON procurement.receipt_posting
FOR EACH ROW EXECUTE FUNCTION procurement.guard_receipt_posting_insert();

-- Atomic creation command: immutable history remains valid after request changes.
CREATE TRIGGER immutable_purchase_request_creation
BEFORE UPDATE OR DELETE ON procurement.purchase_request_creation
FOR EACH ROW EXECUTE FUNCTION procurement.reject_receipt_revision_mutation();
CREATE TRIGGER immutable_purchase_request_creation_truncate
BEFORE TRUNCATE ON procurement.purchase_request_creation
FOR EACH STATEMENT EXECUTE FUNCTION procurement.reject_receipt_revision_mutation();

CREATE OR REPLACE FUNCTION procurement.guard_purchase_request_creation() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE c jsonb; d jsonb; o procurement.purchase_ownership%ROWTYPE;
        r procurement.purchase_request%ROWTYPE; canonical_command text; expected_snapshot jsonb;
        trim_chars CONSTANT text := U&'\0009\000A\000B\000C\000D\001C\001D\001E\001F\0020\0085\00A0\1680\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A\2028\2029\202F\205F\3000';
BEGIN
  c := NEW.command::jsonb;
  d := c->'document';
  IF jsonb_typeof(c) IS DISTINCT FROM 'object'
    OR jsonb_typeof(d) IS DISTINCT FROM 'object'
    OR c IS DISTINCT FROM jsonb_build_object('request_key', NEW.request_key,
         'document', d, 'ownership_evidence', c->'ownership_evidence')
    OR d IS DISTINCT FROM jsonb_build_object('supplier', d->'supplier', 'item', d->'item',
         'qty', d->'qty', 'amount', d->'amount', 'due_date', d->'due_date')
    OR NEW.request_key !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    OR NEW.command_hash !~ '^[0-9a-f]{64}$'
    OR jsonb_typeof(c->'ownership_evidence') IS DISTINCT FROM 'string'
    OR c->>'ownership_evidence' IS DISTINCT FROM btrim(c->>'ownership_evidence', trim_chars)
    OR length(btrim(c->>'ownership_evidence')) NOT BETWEEN 1 AND 1000
    OR jsonb_typeof(d->'supplier') IS DISTINCT FROM 'string'
    OR d->>'supplier' IS DISTINCT FROM btrim(d->>'supplier', trim_chars)
    OR length(btrim(d->>'supplier')) NOT BETWEEN 1 AND 255
    OR jsonb_typeof(d->'item') IS DISTINCT FROM 'string'
    OR d->>'item' IS DISTINCT FROM btrim(d->>'item', trim_chars)
    OR length(btrim(d->>'item')) NOT BETWEEN 1 AND 255
    OR jsonb_typeof(d->'qty') IS DISTINCT FROM 'number'
    OR (NEW.command->'document'->>'qty') !~ '^[1-9][0-9]*$'
    OR jsonb_typeof(d->'amount') IS DISTINCT FROM 'string'
    OR (d->>'amount') !~ '^(0|[1-9][0-9]{0,11})\.[0-9]{2}$'
    OR jsonb_typeof(d->'due_date') NOT IN ('string','null')
    OR (d->>'due_date' IS NOT NULL AND (d->>'due_date') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$') THEN
    RAISE EXCEPTION 'Invalid purchase request creation command';
  END IF;
  IF (d->>'qty')::numeric > 2147483647
    OR (d->>'due_date' IS NOT NULL AND (d->>'due_date')::date::text IS DISTINCT FROM d->>'due_date') THEN
    RAISE EXCEPTION 'Invalid purchase request creation quantities or date';
  END IF;
  canonical_command := '{"document":{"amount":' || (d->'amount')::text
    || ',"due_date":' || (d->'due_date')::text || ',"item":' || (d->'item')::text
    || ',"qty":' || (d->'qty')::text || ',"supplier":' || (d->'supplier')::text
    || '},"ownership_evidence":' || (c->'ownership_evidence')::text
    || ',"request_key":' || (c->'request_key')::text || '}';
  IF NEW.command_hash IS DISTINCT FROM encode(sha256(convert_to(canonical_command, 'UTF8')), 'hex') THEN
    RAISE EXCEPTION 'Purchase request creation command hash mismatch';
  END IF;
  SELECT * INTO o FROM procurement.purchase_ownership WHERE id=NEW.ownership_id;
  SELECT * INTO r FROM procurement.purchase_request WHERE id=NEW.request_id;
  expected_snapshot := jsonb_build_object('number', r.number, 'supplier', d->'supplier',
    'supplier_id', NULL, 'item', d->'item', 'quantity', d->>'qty',
    'planned_amount', d->'amount', 'due_date', d->'due_date');
  IF o.id IS NULL OR r.id IS NULL OR o.kind IS DISTINCT FROM 'request'
    OR o.source_id IS DISTINCT FROM NEW.request_id OR o.organization_id IS DISTINCT FROM NEW.organization_id
    OR o.actor IS DISTINCT FROM NEW.actor OR o.evidence IS DISTINCT FROM c->>'ownership_evidence'
    OR o.snapshot::jsonb IS DISTINCT FROM expected_snapshot
    OR NEW.result::jsonb IS DISTINCT FROM jsonb_build_object('organization_id', NEW.organization_id,
        'request_key', NEW.request_key, 'request_id', NEW.request_id, 'ownership_id', NEW.ownership_id,
        'number', r.number, 'stage', 'need')
    OR r.origin IS DISTINCT FROM ''
    OR r.stage IS DISTINCT FROM 'need' OR r.supplier IS DISTINCT FROM d->>'supplier'
    OR r.item IS DISTINCT FROM d->>'item' OR r.qty IS DISTINCT FROM (d->>'qty')::integer
    OR r.amount IS DISTINCT FROM (d->>'amount')::numeric
    OR r.due_date IS DISTINCT FROM d->>'due_date' OR r.supplier_id IS NOT NULL THEN
    RAISE EXCEPTION 'Purchase request creation ownership or initial result mismatch';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER guard_purchase_request_creation BEFORE INSERT ON procurement.purchase_request_creation
FOR EACH ROW EXECUTE FUNCTION procurement.guard_purchase_request_creation();
