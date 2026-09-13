CREATE TRIGGER immutable_purchase_ownership BEFORE UPDATE OR DELETE ON procurement.purchase_ownership
FOR EACH ROW EXECUTE FUNCTION procurement.reject_receipt_revision_mutation();
CREATE TRIGGER immutable_order_request_link BEFORE UPDATE OR DELETE ON procurement.order_request_link
FOR EACH ROW EXECUTE FUNCTION procurement.reject_receipt_revision_mutation();

CREATE OR REPLACE FUNCTION procurement.guard_order_request_link() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM procurement.purchase_ownership o
    WHERE o.id = NEW.order_ownership_id AND o.kind = 'order' AND o.organization_id = NEW.organization_id)
    OR NOT EXISTS (SELECT 1 FROM procurement.purchase_ownership r
    WHERE r.id = NEW.request_ownership_id AND r.kind = 'request' AND r.organization_id = NEW.organization_id) THEN
    RAISE EXCEPTION 'Order and request must belong to the same organization with the correct roles';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER guard_order_request_link BEFORE INSERT ON procurement.order_request_link
FOR EACH ROW EXECUTE FUNCTION procurement.guard_order_request_link();
