-- Pack sizes for unit pricing ("24 eggs", "16 oz"). As printed (pack_quantity + pack_unit) and
-- converted in code to a base unit: count, g or ml (pack_base_qty + pack_base_unit).
ALTER TABLE item_labels ADD COLUMN pack_quantity REAL;
ALTER TABLE item_labels ADD COLUMN pack_unit TEXT;
ALTER TABLE item_labels ADD COLUMN pack_base_qty REAL;
ALTER TABLE item_labels ADD COLUMN pack_base_unit TEXT CHECK (pack_base_unit IN ('count', 'g', 'ml'));
