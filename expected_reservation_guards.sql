-- Unallocated proposal: install with the expected-reservation tables.
CREATE OR REPLACE FUNCTION procurement.guard_expected_conversion_request() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP <> 'UPDATE' THEN
    RAISE EXCEPTION 'Expected conversion request history cannot be removed';
  END IF;
  IF (to_jsonb(NEW) - 'completed') IS DISTINCT FROM (to_jsonb(OLD) - 'completed')
     OR OLD.completed OR NOT NEW.completed THEN
    RAISE EXCEPTION 'Only pending to completed conversion request transition is allowed';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER guard_expected_conversion_request
BEFORE UPDATE OR DELETE ON procurement.expected_conversion_request
FOR EACH ROW EXECUTE FUNCTION procurement.guard_expected_conversion_request();
CREATE TRIGGER no_truncate_expected_conversion_request
BEFORE TRUNCATE ON procurement.expected_conversion_request
FOR EACH STATEMENT EXECUTE FUNCTION procurement.guard_expected_conversion_request();

CREATE OR REPLACE FUNCTION procurement.guard_expected_reservation_identity() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM procurement.purchase_ownership
                 WHERE organization_id=NEW.organization_id AND kind='order' AND source_id=NEW.order_id)
     OR NOT EXISTS (SELECT 1 FROM procurement.purchase_order_line
                    WHERE id=NEW.order_line_id AND order_id=NEW.order_id)
     OR NOT EXISTS (SELECT 1 FROM sales.deal_ownership
                    WHERE deal_id=NEW.deal_id AND organization_id=NEW.organization_id)
     OR (NEW.demand_id IS NOT NULL AND NOT EXISTS (
          SELECT 1 FROM procurement.deal_procurement_demand d
          JOIN procurement.purchase_order_line l ON l.id=NEW.order_line_id
          WHERE d.id=NEW.demand_id AND d.organization_id=NEW.organization_id
            AND d.deal_id=NEW.deal_id AND d.sku_code=l.sku_code
        ))
     OR (NEW.document_id IS NOT NULL AND NOT EXISTS (
          SELECT 1 FROM sales.deal_document WHERE id=NEW.document_id AND deal_id=NEW.deal_id
            AND kind='invoice')) THEN
    RAISE EXCEPTION 'Expected reservation source identity is not owned and exact';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER guard_expected_reservation_identity
BEFORE INSERT ON procurement.expected_reservation
FOR EACH ROW EXECUTE FUNCTION procurement.guard_expected_reservation_identity();

CREATE OR REPLACE FUNCTION procurement.guard_expected_reservation_event() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM procurement.expected_reservation
                 WHERE id=NEW.reservation_id AND organization_id=NEW.organization_id) THEN
    RAISE EXCEPTION 'Expected reservation event parent differs from organization';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER guard_expected_reservation_event
BEFORE INSERT ON procurement.expected_reservation_event
FOR EACH ROW EXECUTE FUNCTION procurement.guard_expected_reservation_event();

CREATE OR REPLACE FUNCTION procurement.reject_expected_reservation_history_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'Expected reservation history is immutable';
END $$;
CREATE TRIGGER immutable_expected_reservation
BEFORE UPDATE OR DELETE ON procurement.expected_reservation
FOR EACH ROW EXECUTE FUNCTION procurement.reject_expected_reservation_history_mutation();
CREATE TRIGGER immutable_physical_receipt_acceptance
BEFORE UPDATE OR DELETE ON procurement.physical_receipt_acceptance
FOR EACH ROW EXECUTE FUNCTION procurement.reject_expected_reservation_history_mutation();
CREATE TRIGGER no_truncate_physical_receipt_acceptance
BEFORE TRUNCATE ON procurement.physical_receipt_acceptance
FOR EACH STATEMENT EXECUTE FUNCTION procurement.reject_expected_reservation_history_mutation();
CREATE TRIGGER immutable_expected_reservation_event
BEFORE UPDATE OR DELETE ON procurement.expected_reservation_event
FOR EACH ROW EXECUTE FUNCTION procurement.reject_expected_reservation_history_mutation();
CREATE TRIGGER no_truncate_expected_reservation
BEFORE TRUNCATE ON procurement.expected_reservation
FOR EACH STATEMENT EXECUTE FUNCTION procurement.reject_expected_reservation_history_mutation();
CREATE TRIGGER no_truncate_expected_reservation_event
BEFORE TRUNCATE ON procurement.expected_reservation_event
FOR EACH STATEMENT EXECUTE FUNCTION procurement.reject_expected_reservation_history_mutation();
