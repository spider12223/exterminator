def parse_row(row):
    name, price = row.split(",")
    return {"name": name.strip(), "price": float(price)}


def import_products(rows, db):
    """Insert product rows into the catalog. Returns the number imported."""
    imported = 0
    for row in rows:
        product = parse_row(row)
        try:
            db.insert(product)
        except:
            continue
        imported += 1
    return imported
