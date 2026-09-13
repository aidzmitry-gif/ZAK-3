-- Pending integration with purchase_order_creation; do not install separately.
-- INSERT provenance is separate from xmin: an UPDATE also changes xmin, and
-- savepoints use subtransaction xids. Only the order's INSERT trigger records it.
CREATE TABLE procurement.order_insert_proof (
  order_id integer PRIMARY KEY,
  root_transaction bigint NOT NULL
);
CREATE INDEX ix_order_insert_proof_transaction ON procurement.order_insert_proof(root_transaction);
CREATE OR REPLACE FUNCTION procurement.guard_order_insert_proof() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF pg_trigger_depth() <> 2 OR NEW.root_transaction IS DISTINCT FROM txid_current()
    OR NOT EXISTS (SELECT 1 FROM procurement.purchase_order WHERE id=NEW.order_id) THEN
    RAISE EXCEPTION 'Order insertion proof must originate from the order INSERT trigger';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER guard_order_insert_proof BEFORE INSERT ON procurement.order_insert_proof
FOR EACH ROW EXECUTE FUNCTION procurement.guard_order_insert_proof();
CREATE TRIGGER immutable_order_insert_proof BEFORE UPDATE OR DELETE ON procurement.order_insert_proof
FOR EACH ROW EXECUTE FUNCTION procurement.reject_receipt_revision_mutation();
CREATE TRIGGER immutable_order_insert_proof_truncate BEFORE TRUNCATE ON procurement.order_insert_proof
FOR EACH STATEMENT EXECUTE FUNCTION procurement.reject_receipt_revision_mutation();
CREATE OR REPLACE FUNCTION procurement.record_order_insert() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO procurement.order_insert_proof(order_id,root_transaction) VALUES (NEW.id,txid_current());
  RETURN NEW;
END $$;
CREATE TRIGGER record_order_insert AFTER INSERT ON procurement.purchase_order
FOR EACH ROW EXECUTE FUNCTION procurement.record_order_insert();

CREATE OR REPLACE FUNCTION procurement.keep_order_line_parent() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.order_id IS DISTINCT FROM OLD.order_id THEN
    RAISE EXCEPTION 'Order line parent is immutable; create a new line in the target order';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER keep_order_line_parent BEFORE UPDATE OF order_id ON procurement.purchase_order_line
FOR EACH ROW EXECUTE FUNCTION procurement.keep_order_line_parent();

-- All object keys below are fixed ASCII; numbers are validated integer identifiers.
CREATE OR REPLACE FUNCTION procurement.order_command_json(value jsonb) RETURNS text
LANGUAGE plpgsql IMMUTABLE STRICT AS $$
DECLARE result text;
BEGIN
  CASE jsonb_typeof(value)
    WHEN 'object' THEN
      SELECT '{' || coalesce(string_agg(to_jsonb(key)::text || ':' ||
        procurement.order_command_json(val), ',' ORDER BY key COLLATE "C"), '') || '}'
        INTO result FROM jsonb_each(value) AS pairs(key,val);
    WHEN 'array' THEN
      SELECT '[' || coalesce(string_agg(procurement.order_command_json(val), ',' ORDER BY ordinal), '') || ']'
        INTO result FROM jsonb_array_elements(value) WITH ORDINALITY AS items(val,ordinal);
    ELSE result := value::text;
  END CASE;
  RETURN result;
END $$;

CREATE OR REPLACE FUNCTION procurement.order_command_text(value jsonb, max_length integer) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
  SELECT coalesce(jsonb_typeof(value) = 'string' AND length(value #>> '{}') BETWEEN 1 AND max_length
    AND (value #>> '{}') = btrim(value #>> '{}',
      U&'\0009\000A\000B\000C\000D\001C\001D\001E\001F\0020\0085\00A0\1680\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A\2028\2029\202F\205F\3000'), false)
$$;

-- Immutable evidence of the actual pre-transition state, not a reconstruction
-- of an approval stage from a request which is already linked to an order.
CREATE TABLE procurement.order_request_transition_proof (
  request_id integer NOT NULL,
  root_transaction bigint NOT NULL,
  snapshot jsonb NOT NULL,
  PRIMARY KEY (request_id, root_transaction)
);
CREATE OR REPLACE FUNCTION procurement.guard_order_request_transition_proof() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF pg_trigger_depth() <> 2 OR NEW.root_transaction IS DISTINCT FROM txid_current()
    OR NOT EXISTS (SELECT 1 FROM procurement.purchase_request WHERE id=NEW.request_id AND stage='approval') THEN
    RAISE EXCEPTION 'Approval proof must originate from the request transition trigger';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER guard_order_request_transition_proof BEFORE INSERT ON procurement.order_request_transition_proof
FOR EACH ROW EXECUTE FUNCTION procurement.guard_order_request_transition_proof();
CREATE TRIGGER immutable_order_request_transition_proof BEFORE UPDATE OR DELETE ON procurement.order_request_transition_proof
FOR EACH ROW EXECUTE FUNCTION procurement.reject_receipt_revision_mutation();
CREATE TRIGGER immutable_order_request_transition_proof_truncate BEFORE TRUNCATE ON procurement.order_request_transition_proof
FOR EACH STATEMENT EXECUTE FUNCTION procurement.reject_receipt_revision_mutation();
CREATE OR REPLACE FUNCTION procurement.record_order_request_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE owner_id integer;
BEGIN
  IF OLD.stage='approval' AND NEW.stage='po' THEN
    SELECT id INTO owner_id FROM procurement.purchase_ownership WHERE kind='request' AND source_id=OLD.id;
    IF owner_id IS NOT NULL THEN
      INSERT INTO procurement.order_request_transition_proof(request_id,root_transaction,snapshot)
      VALUES (OLD.id,txid_current(),jsonb_build_object('request_id',OLD.id,'ownership_id',owner_id,
        'number',OLD.number,'supplier',OLD.supplier,'supplier_id',OLD.supplier_id,'item',OLD.item,
        'qty',OLD.qty::text,'amount',OLD.amount::text,'due_date',OLD.due_date,'stage',OLD.stage));
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER record_order_request_transition BEFORE UPDATE OF stage ON procurement.purchase_request
FOR EACH ROW EXECUTE FUNCTION procurement.record_order_request_transition();

CREATE OR REPLACE FUNCTION procurement.guard_order_creation() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE c jsonb; d jsonb; b jsonb; item jsonb; actual_lines jsonb; expected jsonb;
        snapshot jsonb; proof_snapshot jsonb; position_count integer; index_number integer; id_key text; o procurement.purchase_order%ROWTYPE;
        owner_row procurement.purchase_ownership%ROWTYPE; request_owner procurement.purchase_ownership%ROWTYPE;
        request_row procurement.purchase_request%ROWTYPE; link_row procurement.order_request_link%ROWTYPE;
BEGIN
  PERFORM id FROM accounting.organization WHERE id=NEW.organization_id FOR UPDATE;
  c := NEW.command::jsonb; d := c->'document'; b := c->'request_basis';
  IF jsonb_typeof(c) IS DISTINCT FROM 'object' OR jsonb_typeof(d) IS DISTINCT FROM 'object'
    OR c IS DISTINCT FROM jsonb_build_object('request_key',NEW.request_key,'document',d,
         'ownership_evidence',c->'ownership_evidence','request_basis',b)
    OR d IS DISTINCT FROM jsonb_build_object('supplier',d->'supplier','eta_date',d->'eta_date',
         'freight_byn',d->'freight_byn','lines',d->'lines')
    OR NEW.request_key !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    OR NEW.command_hash !~ '^[0-9a-f]{64}$'
    OR NEW.actor IS NULL OR length(NEW.actor) NOT BETWEEN 1 AND 200
    OR NOT procurement.order_command_text(c->'ownership_evidence',1000)
    OR NOT procurement.order_command_text(d->'supplier',255)
    OR jsonb_typeof(d->'lines') IS DISTINCT FROM 'array'
    OR jsonb_typeof(d->'freight_byn') IS DISTINCT FROM 'string'
    OR (d->>'freight_byn') !~ '^(0|[1-9][0-9]{0,11})\.[0-9]{2}$'
    OR jsonb_typeof(d->'eta_date') NOT IN ('null','string')
    OR (d->>'eta_date' IS NOT NULL AND (d->>'eta_date') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$')
    OR jsonb_typeof(b) NOT IN ('null','object') THEN
    RAISE EXCEPTION 'Invalid order creation command';
  END IF;
  IF jsonb_array_length(d->'lines') NOT BETWEEN 1 AND 200
    OR (d->>'eta_date' IS NOT NULL AND (d->>'eta_date')::date::text IS DISTINCT FROM d->>'eta_date') THEN
    RAISE EXCEPTION 'Invalid order creation line count or date';
  END IF;
  FOR item IN SELECT value FROM jsonb_array_elements(d->'lines') LOOP
    IF item IS DISTINCT FROM jsonb_build_object('sku_code',item->'sku_code','qty',item->'qty',
        'goods_value_byn',item->'goods_value_byn','weight',item->'weight','volume',item->'volume')
      OR NOT procurement.order_command_text(item->'sku_code',64)
      OR jsonb_typeof(item->'qty') IS DISTINCT FROM 'string'
      OR (item->>'qty') !~ '^(0|[1-9][0-9]{0,11})\.[0-9]{2}$'
      OR jsonb_typeof(item->'goods_value_byn') IS DISTINCT FROM 'string'
      OR (item->>'goods_value_byn') !~ '^(0|[1-9][0-9]{0,11})\.[0-9]{2}$'
      OR jsonb_typeof(item->'weight') IS DISTINCT FROM 'string'
      OR (item->>'weight') !~ '^(0|[1-9][0-9]{0,10})\.[0-9]{3}$'
      OR jsonb_typeof(item->'volume') IS DISTINCT FROM 'string'
      OR (item->>'volume') !~ '^(0|[1-9][0-9]{0,9})\.[0-9]{4}$' THEN
      RAISE EXCEPTION 'Invalid order creation line';
    END IF;
    IF (item->>'qty')::numeric <= 0 THEN RAISE EXCEPTION 'Invalid order creation quantity'; END IF;
  END LOOP;
  SELECT count(DISTINCT value->>'sku_code') INTO position_count FROM jsonb_array_elements(d->'lines');
  IF position_count <> jsonb_array_length(d->'lines') THEN RAISE EXCEPTION 'Duplicate order creation SKU'; END IF;
  IF b <> 'null'::jsonb THEN
    IF b IS DISTINCT FROM jsonb_build_object('request_id',b->'request_id','expected_stage','approval',
         'expected_hash',b->'expected_hash','link_evidence',b->'link_evidence')
      OR jsonb_typeof(b->'request_id') IS DISTINCT FROM 'number'
      OR (NEW.command->'request_basis'->>'request_id') !~ '^[1-9][0-9]*$'
      OR jsonb_typeof(b->'expected_hash') IS DISTINCT FROM 'string'
      OR (b->>'expected_hash') !~ '^[0-9a-f]{64}$'
      OR NOT procurement.order_command_text(b->'link_evidence',1000) THEN
      RAISE EXCEPTION 'Invalid order creation request basis';
    END IF;
    IF (b->>'request_id')::numeric > 2147483647 THEN RAISE EXCEPTION 'Invalid order creation request identifier'; END IF;
  END IF;
  IF NEW.command_hash IS DISTINCT FROM encode(sha256(convert_to(procurement.order_command_json(c),'UTF8')),'hex') THEN
    RAISE EXCEPTION 'Order creation command hash mismatch';
  END IF;
  expected := jsonb_build_object('organization_id',NEW.organization_id,'request_key',NEW.request_key,
                                'principal',NEW.actor,'outcome',NEW.outcome);
  IF coalesce(NEW.result->>'organization_id','') !~ '^[1-9][0-9]*$' THEN
    RAISE EXCEPTION 'Invalid order creation result identifier';
  END IF;
  IF NEW.outcome = 'rejected' THEN
    -- Each HTTP command owns one root transaction; a rejection may not leave
    -- an order inserted earlier or later in that transaction (deferred recheck).
    IF NEW.order_id IS NOT NULL OR NEW.ownership_id IS NOT NULL OR NEW.request_id IS NOT NULL
      OR NEW.request_ownership_id IS NOT NULL OR NEW.link_id IS NOT NULL
      OR coalesce(NEW.result->>'code','') NOT IN
         ('request_basis_changed','request_basis_unavailable','request_already_linked','command_abandoned')
      OR (NEW.result->>'code' <> 'command_abandoned' AND b = 'null'::jsonb)
      OR EXISTS (SELECT 1 FROM procurement.order_insert_proof WHERE root_transaction=txid_current())
      OR NEW.result::jsonb IS DISTINCT FROM expected || jsonb_build_object('code',NEW.result->>'code','no_business_write',true) THEN
      RAISE EXCEPTION 'Invalid rejected order creation outcome';
    END IF;
    RETURN NEW;
  END IF;
  IF NEW.outcome IS DISTINCT FROM 'created' THEN RAISE EXCEPTION 'Invalid order creation outcome'; END IF;
  SELECT * INTO o FROM procurement.purchase_order WHERE id=NEW.order_id;
  SELECT * INTO owner_row FROM procurement.purchase_ownership WHERE id=NEW.ownership_id;
  IF o.id IS NULL OR owner_row.id IS NULL OR owner_row.kind IS DISTINCT FROM 'order'
    OR owner_row.source_id IS DISTINCT FROM o.id OR owner_row.organization_id IS DISTINCT FROM NEW.organization_id
    OR owner_row.actor IS DISTINCT FROM NEW.actor OR owner_row.evidence IS DISTINCT FROM c->>'ownership_evidence'
    OR o.status IS DISTINCT FROM 'draft' OR o.received_at IS NOT NULL OR o.supplier_id IS NOT NULL
    OR o.transport_method_code IS NOT NULL OR o.target_arrival_date IS NOT NULL
    OR o.supplier IS DISTINCT FROM d->>'supplier' OR o.freight_byn IS DISTINCT FROM (d->>'freight_byn')::numeric
    OR o.eta_date::text IS DISTINCT FROM d->>'eta_date'
    OR owner_row.snapshot::jsonb IS DISTINCT FROM jsonb_build_object('number',o.number,'supplier',o.supplier,
         'supplier_id',NULL,'status','draft','eta_date',d->'eta_date') THEN
    RAISE EXCEPTION 'Order creation initial ownership/header mismatch';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM procurement.order_insert_proof WHERE order_id=o.id AND root_transaction=txid_current()) THEN
    RAISE EXCEPTION 'Order creation package must originate in this transaction';
  END IF;
  SELECT jsonb_agg(jsonb_build_object('id',id,'sku_code',sku_code,'qty',qty::text,
    'goods_value_byn',goods_value_byn::text,'weight',weight::text,'volume',volume::text) ORDER BY id)
    INTO actual_lines FROM procurement.purchase_order_line WHERE order_id=o.id;
  IF (SELECT jsonb_agg(value - 'id' ORDER BY ordinal)
      FROM jsonb_array_elements(actual_lines) WITH ORDINALITY AS lines(value,ordinal)) IS DISTINCT FROM d->'lines' THEN
    RAISE EXCEPTION 'Order creation initial lines mismatch';
  END IF;
  snapshot := 'null'::jsonb;
  IF b = 'null'::jsonb THEN
    IF NEW.request_id IS NOT NULL OR NEW.request_ownership_id IS NOT NULL OR NEW.link_id IS NOT NULL THEN
      RAISE EXCEPTION 'Unexpected order creation request link';
    END IF;
  ELSE
    SELECT * INTO request_row FROM procurement.purchase_request WHERE id=NEW.request_id;
    SELECT * INTO request_owner FROM procurement.purchase_ownership WHERE id=NEW.request_ownership_id;
    SELECT * INTO link_row FROM procurement.order_request_link WHERE id=NEW.link_id;
    SELECT proof.snapshot INTO proof_snapshot FROM procurement.order_request_transition_proof proof
      WHERE proof.request_id=NEW.request_id AND proof.root_transaction=txid_current();
    snapshot := jsonb_build_object('request_id',request_row.id,'ownership_id',request_owner.id,
      'number',request_row.number,'supplier',request_row.supplier,'supplier_id',request_row.supplier_id,
      'item',request_row.item,'qty',request_row.qty::text,'amount',request_row.amount::text,
      'due_date',request_row.due_date,'stage','approval');
    IF request_row.id IS NULL OR request_owner.id IS NULL OR link_row.id IS NULL
      OR NEW.request_id IS DISTINCT FROM (b->>'request_id')::integer OR request_row.stage IS DISTINCT FROM 'po'
      OR request_owner.kind IS DISTINCT FROM 'request' OR request_owner.source_id IS DISTINCT FROM NEW.request_id
      OR request_owner.organization_id IS DISTINCT FROM NEW.organization_id
      OR link_row.organization_id IS DISTINCT FROM NEW.organization_id OR link_row.actor IS DISTINCT FROM NEW.actor
      OR link_row.order_ownership_id IS DISTINCT FROM NEW.ownership_id
      OR link_row.request_ownership_id IS DISTINCT FROM NEW.request_ownership_id
      OR link_row.evidence IS DISTINCT FROM b->>'link_evidence'
      OR (SELECT count(*) FROM procurement.order_request_link WHERE request_ownership_id=NEW.request_ownership_id) <> 1
      OR proof_snapshot IS DISTINCT FROM snapshot
      OR encode(sha256(convert_to(procurement.order_command_json(snapshot),'UTF8')),'hex') IS DISTINCT FROM b->>'expected_hash' THEN
      RAISE EXCEPTION 'Order creation approved request/link mismatch';
    END IF;
  END IF;
  expected := expected || jsonb_build_object('order_id',o.id,'ownership_id',NEW.ownership_id,'number',o.number,
    'status','draft','supplier',o.supplier,'eta_date',d->'eta_date','freight_byn',d->'freight_byn',
    'lines',actual_lines,'request_id',NEW.request_id,'request_ownership_id',NEW.request_ownership_id,
    'link_id',NEW.link_id,'request_snapshot',snapshot);
  IF NEW.result::jsonb IS DISTINCT FROM expected THEN RAISE EXCEPTION 'Order creation result mismatch'; END IF;
  FOREACH id_key IN ARRAY ARRAY['order_id','ownership_id','request_id','request_ownership_id','link_id'] LOOP
    IF NEW.result->>id_key IS NOT NULL AND (NEW.result->>id_key) !~ '^[1-9][0-9]*$' THEN
      RAISE EXCEPTION 'Invalid order creation result identifier';
    END IF;
  END LOOP;
  FOR index_number IN 0..jsonb_array_length(actual_lines)-1 LOOP
    IF coalesce(NEW.result->'lines'->index_number->>'id','') !~ '^[1-9][0-9]*$' THEN
      RAISE EXCEPTION 'Invalid order creation result line identifier';
    END IF;
  END LOOP;
  IF b <> 'null'::jsonb THEN
    FOREACH id_key IN ARRAY ARRAY['request_id','ownership_id','supplier_id'] LOOP
      IF NEW.result->'request_snapshot'->>id_key IS NOT NULL
        AND (NEW.result->'request_snapshot'->>id_key) !~ '^[1-9][0-9]*$' THEN
        RAISE EXCEPTION 'Invalid order creation snapshot identifier';
      END IF;
    END LOOP;
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER guard_order_creation BEFORE INSERT ON procurement.purchase_order_creation
FOR EACH ROW EXECUTE FUNCTION procurement.guard_order_creation();
CREATE CONSTRAINT TRIGGER complete_order_creation AFTER INSERT ON procurement.purchase_order_creation
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION procurement.guard_order_creation();
CREATE TRIGGER immutable_order_creation BEFORE UPDATE OR DELETE ON procurement.purchase_order_creation
FOR EACH ROW EXECUTE FUNCTION procurement.reject_receipt_revision_mutation();
CREATE TRIGGER immutable_order_creation_truncate BEFORE TRUNCATE ON procurement.purchase_order_creation
FOR EACH STATEMENT EXECUTE FUNCTION procurement.reject_receipt_revision_mutation();
