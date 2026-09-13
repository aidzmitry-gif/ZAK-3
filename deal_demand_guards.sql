-- Unallocated proposal: install with the deal-demand register.
CREATE OR REPLACE FUNCTION procurement.guard_deal_procurement_demand() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  item_qty numeric;
BEGIN
  IF NOT EXISTS (
       SELECT 1 FROM sales.deal_ownership
       WHERE deal_id = NEW.deal_id AND organization_id = NEW.organization_id
     )
     OR NOT EXISTS (
       SELECT 1 FROM sales.deal_item
       WHERE id = NEW.deal_item_id AND deal_id = NEW.deal_id AND sku_id = NEW.sku_id
     )
     OR NOT EXISTS (
       SELECT 1 FROM public.sku
       WHERE id = NEW.sku_id AND code = NEW.sku_code AND is_active
     )
     OR (NEW.document_id IS NOT NULL AND NOT EXISTS (
       SELECT 1 FROM sales.deal_document
       WHERE id = NEW.document_id AND deal_id = NEW.deal_id AND kind = 'invoice'
     )) THEN
    RAISE EXCEPTION 'Deal procurement demand source identity is not exact and owned';
  END IF;

  SELECT qty INTO item_qty
  FROM sales.deal_item
  WHERE id = NEW.deal_item_id AND deal_id = NEW.deal_id
  FOR UPDATE;
  IF item_qty IS NULL OR NEW.qty + COALESCE((
       SELECT sum(qty) FROM procurement.deal_procurement_demand
       WHERE organization_id = NEW.organization_id AND deal_item_id = NEW.deal_item_id
     ), 0) > item_qty THEN
    RAISE EXCEPTION 'Deal procurement demand exceeds the CRM item quantity';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER guard_deal_procurement_demand
BEFORE INSERT ON procurement.deal_procurement_demand
FOR EACH ROW EXECUTE FUNCTION procurement.guard_deal_procurement_demand();

CREATE OR REPLACE FUNCTION procurement.guard_deal_procurement_allocation() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  demand_qty numeric;
  demand_sku text;
  order_status text;
  line_qty numeric;
  line_sku text;
BEGIN
  SELECT qty, sku_code INTO demand_qty, demand_sku
  FROM procurement.deal_procurement_demand
  WHERE id = NEW.demand_id AND organization_id = NEW.organization_id
  FOR UPDATE;
  SELECT po.status, pol.qty, pol.sku_code INTO STRICT order_status, line_qty, line_sku
  FROM procurement.purchase_order_line pol
  JOIN procurement.purchase_order po ON po.id = pol.order_id
  WHERE pol.id = NEW.order_line_id AND pol.order_id = NEW.order_id;
  IF NOT EXISTS (
       SELECT 1 FROM procurement.purchase_ownership
       WHERE organization_id = NEW.organization_id AND kind = 'order' AND source_id = NEW.order_id
     )
     OR demand_qty IS NULL
     OR line_sku IS NULL
     OR line_sku <> demand_sku
     OR order_status NOT IN ('ordered', 'shipped', 'customs') THEN
    RAISE EXCEPTION 'Deal demand allocation source identity is not exact and open';
  END IF;
  IF NEW.qty + GREATEST(COALESCE((
       SELECT sum(qty) FROM procurement.deal_procurement_allocation
       WHERE organization_id = NEW.organization_id AND demand_id = NEW.demand_id
     ), 0) - COALESCE((
       SELECT sum(e.qty)
       FROM procurement.expected_reservation r
       JOIN procurement.expected_reservation_event e ON e.reservation_id = r.id
       WHERE r.organization_id = NEW.organization_id AND r.demand_id = NEW.demand_id
         AND e.organization_id = NEW.organization_id AND e.kind = 'release'
     ), 0), 0) > demand_qty THEN
    RAISE EXCEPTION 'Demand allocation exceeds the client demand';
  END IF;
  IF NEW.qty + COALESCE((
       SELECT sum(r.qty - COALESCE((
         SELECT sum(e.qty) FROM procurement.expected_reservation_event e
         WHERE e.reservation_id = r.id AND e.organization_id = NEW.organization_id
           AND e.kind = 'release'
       ), 0))
       FROM procurement.expected_reservation r
       WHERE r.organization_id = NEW.organization_id AND r.order_line_id = NEW.order_line_id
     ), 0) + COALESCE((
       SELECT sum(a.qty) FROM procurement.deal_procurement_allocation a
       WHERE a.organization_id = NEW.organization_id AND a.order_line_id = NEW.order_line_id
         AND NOT EXISTS (
           SELECT 1 FROM procurement.expected_reservation r
           WHERE r.organization_id = NEW.organization_id AND r.demand_id = a.demand_id
             AND r.order_line_id = a.order_line_id
         )
     ), 0) > line_qty THEN
    RAISE EXCEPTION 'Demand allocation exceeds the supplier order line';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER guard_deal_procurement_allocation
BEFORE INSERT ON procurement.deal_procurement_allocation
FOR EACH ROW EXECUTE FUNCTION procurement.guard_deal_procurement_allocation();

CREATE OR REPLACE FUNCTION procurement.reject_deal_procurement_history_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'Deal procurement history is immutable';
END $$;
CREATE TRIGGER immutable_deal_procurement_demand
BEFORE UPDATE OR DELETE ON procurement.deal_procurement_demand
FOR EACH ROW EXECUTE FUNCTION procurement.reject_deal_procurement_history_mutation();
CREATE TRIGGER immutable_deal_procurement_allocation
BEFORE UPDATE OR DELETE ON procurement.deal_procurement_allocation
FOR EACH ROW EXECUTE FUNCTION procurement.reject_deal_procurement_history_mutation();
CREATE TRIGGER no_truncate_deal_procurement_demand
BEFORE TRUNCATE ON procurement.deal_procurement_demand
FOR EACH STATEMENT EXECUTE FUNCTION procurement.reject_deal_procurement_history_mutation();
CREATE TRIGGER no_truncate_deal_procurement_allocation
BEFORE TRUNCATE ON procurement.deal_procurement_allocation
FOR EACH STATEMENT EXECUTE FUNCTION procurement.reject_deal_procurement_history_mutation();

CREATE OR REPLACE FUNCTION procurement.guard_supplier_order_line_allocations() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE allocated numeric;
        expected_reserved numeric;
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF EXISTS (SELECT 1 FROM procurement.deal_procurement_allocation
               WHERE order_line_id = OLD.id)
       OR EXISTS (SELECT 1 FROM procurement.expected_reservation
                  WHERE order_line_id = OLD.id) THEN
      RAISE EXCEPTION 'Supplier order line has client allocations or expected reservations';
    END IF;
    RETURN OLD;
  END IF;
  IF NEW.id <> OLD.id OR NEW.order_id <> OLD.order_id OR NEW.sku_code <> OLD.sku_code THEN
    IF EXISTS (SELECT 1 FROM procurement.deal_procurement_allocation WHERE order_line_id = OLD.id)
       OR EXISTS (SELECT 1 FROM procurement.expected_reservation WHERE order_line_id = OLD.id) THEN
      RAISE EXCEPTION 'Supplier order line identity is immutable after client allocation';
    END IF;
  END IF;
  SELECT COALESCE(sum(r.qty - COALESCE((
    SELECT sum(e.qty) FROM procurement.expected_reservation_event e
    WHERE e.reservation_id = r.id AND e.kind = 'release'
  ), 0)), 0)
  INTO expected_reserved
  FROM procurement.expected_reservation r
  WHERE r.order_line_id = OLD.id;
  SELECT COALESCE(sum(a.qty), 0) INTO allocated
  FROM procurement.deal_procurement_allocation a
  WHERE a.order_line_id = OLD.id
    AND NOT EXISTS (
      SELECT 1 FROM procurement.expected_reservation r
      WHERE r.organization_id = a.organization_id AND r.demand_id = a.demand_id
        AND r.order_line_id = a.order_line_id
    );
  IF NEW.qty < allocated OR NEW.qty < expected_reserved THEN
    RAISE EXCEPTION 'Supplier order line quantity is below client allocation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER guard_supplier_order_line_allocations
BEFORE UPDATE OR DELETE ON procurement.purchase_order_line
FOR EACH ROW EXECUTE FUNCTION procurement.guard_supplier_order_line_allocations();
