"""Tests for the Shopee XLSX parser (apps/channels/parsers/shopee.py)."""
import io
from decimal import Decimal

import openpyxl
import pytest

from apps.channels.parsers.shopee import parse_shopee_xlsx
from apps.channels.services import ChannelImportError


def _make_xlsx(rows, headers=None):
    """Build an in-memory XLSX file with the given rows.
    headers defaults to the standard 50-column Shopee header.
    Returns a file-like BytesIO ready for parse_shopee_xlsx().
    """
    if headers is None:
        headers = [
            "No. Pesanan", "Status Pesanan", "Alasan Pembatalan",
            "Status Pembatalan/ Pengembalian", "No. Resi", "Opsi Pengiriman",
            "Antar ke counter/ pick-up", "Pesanan Harus Dikirimkan Sebelum (Menghindari keterlambatan)",
            "Waktu Pengiriman Diatur", "Waktu Pesanan Dibuat", "Waktu Pembayaran Dilakukan",
            "Tipe Pesanan", "Metode Pembayaran", "SKU Induk", "Nama Produk",
            "Nomor Referensi SKU", "Nama Variasi", "Harga Awal", "Harga Setelah Diskon",
            "Jumlah", "Returned quantity", "Subtotal Pesanan", "Total Diskon",
            "Diskon Dari Penjual", "Diskon Dari Shopee", "Berat Produk",
            "Jumlah Produk di Pesan", "Total Berat", "Voucher Ditanggung Penjual",
            "Cashback Koin", "Voucher Ditanggung Shopee", "Paket Diskon",
            "Paket Diskon (Diskon dari Shopee)", "Paket Diskon (Diskon dari Penjual)",
            "Potongan Koin Shopee", "Diskon Kartu Kredit",
            "Ongkos Kirim Dibayar oleh Pembeli", "Estimasi Potongan Biaya Pengiriman",
            "Ongkos Kirim Pengembalian Barang", "Total Pembayaran", "Perkiraan Ongkos Kirim",
            "Catatan dari Pembeli", "Catatan", "Username (Pembeli)", "Nama Penerima",
            "No. Telepon", "Alamat Pengiriman", "Kota/Kabupaten", "Provinsi",
            "Waktu Pesanan Selesai",
        ]
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "orders"
    ws.append(headers)
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def _row(order_id, status, product_name, sku_ref="", sku_parent="", variation="",
         unit_price="100.000", quantity="1", seller_discount="0", shipping_cost="0",
         total="100.000", tracking="", courier="Standar-JNE", username="user1",
         recipient="A******i", phone="******01", address="Jl. Test", city="JAKARTA",
         province="DKI JAKARTA"):
    """Build one Shopee XLSX data row (50 columns, same order as Shopee export)."""
    return [
        order_id,       # No. Pesanan
        status,         # Status Pesanan
        "",             # Alasan Pembatalan
        "",             # Status Pembatalan/Pengembalian
        tracking,       # No. Resi
        courier,        # Opsi Pengiriman
        "Pickup",       # Antar ke counter/pick-up
        "",             # Pesanan Harus Dikirimkan Sebelum
        "",             # Waktu Pengiriman Diatur
        "2025-12-01 10:00",  # Waktu Pesanan Dibuat
        "2025-12-01 10:01",  # Waktu Pembayaran Dilakukan
        "",             # Tipe Pesanan
        "Online Payment",  # Metode Pembayaran
        sku_parent,     # SKU Induk
        product_name,   # Nama Produk
        sku_ref,        # Nomor Referensi SKU
        variation,      # Nama Variasi
        unit_price,     # Harga Awal
        unit_price,     # Harga Setelah Diskon
        quantity,       # Jumlah
        "0",            # Returned quantity
        unit_price,     # Subtotal Pesanan
        "0",            # Total Diskon
        seller_discount,  # Diskon Dari Penjual
        "0",            # Diskon Dari Shopee
        "500 gr",       # Berat Produk
        quantity,       # Jumlah Produk di Pesan
        "500 gr",       # Total Berat
        "0", "0", "0", "N", "0", "0", "0", "0",  # vouchers/discounts
        shipping_cost,  # Ongkos Kirim Dibayar oleh Pembeli
        "0",            # Estimasi Potongan Biaya Pengiriman
        "0",            # Ongkos Kirim Pengembalian Barang
        total,          # Total Pembayaran
        "6.500",        # Perkiraan Ongkos Kirim
        "",             # Catatan dari Pembeli
        "",             # Catatan
        username,       # Username (Pembeli)
        recipient,      # Nama Penerima
        phone,          # No. Telepon
        address,        # Alamat Pengiriman
        city,           # Kota/Kabupaten
        province,       # Provinsi
        "",             # Waktu Pesanan Selesai
    ]


class TestParseShopeeXlsx:

    def test_parses_single_completed_order(self):
        xlsx = _make_xlsx([
            _row("SHP001", "Selesai", "Tumbler 900ml", sku_ref="TBL-001",
                 tracking="CM123", courier="Reguler-Ninja Xpress",
                 total="100.000", shipping_cost="10.000", username="buyer1"),
        ])
        orders, skipped = parse_shopee_xlsx(xlsx)
        assert len(orders) == 1
        assert skipped == 0
        o = orders[0]
        assert o.external_id == "SHP001"
        assert o.tracking_number == "CM123"
        assert o.courier == "Ninja Xpress"
        assert len(o.lines) == 1
        assert o.lines[0].sku == "TBL-001"
        assert o.lines[0].unit_price == Decimal("100000")
        assert o.lines[0].quantity == 1
        assert o.shipping_cost == Decimal("10000")

    def test_keeps_cancelled_orders_with_their_state(self):
        xlsx = _make_xlsx([
            _row("SHP001", "Batal", "Tumbler", sku_ref="TBL-001"),
            _row("SHP002", "Selesai", "Tumbler", sku_ref="TBL-001"),
        ])
        orders, skipped = parse_shopee_xlsx(xlsx)
        assert skipped == 0
        assert [(o.external_id, o.state) for o in orders] == [
            ("SHP001", "cancelled"), ("SHP002", "completed")]

    def test_groups_multi_product_order(self):
        xlsx = _make_xlsx([
            _row("SHP001", "Selesai", "Produk A", sku_ref="SKU-A", quantity="2"),
            _row("SHP001", "Selesai", "Produk B", sku_ref="SKU-B", quantity="1"),
        ])
        orders, skipped = parse_shopee_xlsx(xlsx)
        assert len(orders) == 1
        assert len(orders[0].lines) == 2
        skus = {l.sku for l in orders[0].lines}
        assert skus == {"SKU-A", "SKU-B"}

    def test_uses_sku_induk_when_nomor_referensi_empty(self):
        xlsx = _make_xlsx([
            _row("SHP001", "Selesai", "Tumbler", sku_ref="", sku_parent="TBL-PARENT"),
        ])
        orders, _ = parse_shopee_xlsx(xlsx)
        assert orders[0].lines[0].sku == "TBL-PARENT"

    def test_placeholder_sku_when_both_empty(self):
        xlsx = _make_xlsx([
            _row("SHP001", "Selesai", "Tumbler 900ml", sku_ref="", sku_parent="",
                 variation="Pink Flower"),
        ])
        orders, _ = parse_shopee_xlsx(xlsx)
        sku = orders[0].lines[0].sku
        assert sku.startswith("[Tanpa SKU:")
        assert "Tumbler 900ml" in sku

    def test_uses_username_as_phone(self):
        xlsx = _make_xlsx([
            _row("SHP001", "Selesai", "Tumbler", sku_ref="S1", username="shopeebuyer99",
                 phone="******99"),
        ])
        orders, _ = parse_shopee_xlsx(xlsx)
        assert orders[0].phone == "shopeebuyer99"

    def test_masked_name_falls_back_to_username(self):
        xlsx = _make_xlsx([
            _row("SHP001", "Selesai", "Tumbler", sku_ref="S1", username="user123",
                 recipient="A******i"),
        ])
        orders, _ = parse_shopee_xlsx(xlsx)
        assert "@user123" in orders[0].buyer_name

    def test_unmasked_name_used_directly(self):
        xlsx = _make_xlsx([
            _row("SHP001", "Selesai", "Tumbler", sku_ref="S1", username="u1",
                 recipient="Andi Budi"),
        ])
        orders, _ = parse_shopee_xlsx(xlsx)
        assert orders[0].buyer_name == "Andi Budi"

    def test_courier_extracted_from_opsi_pengiriman(self):
        xlsx = _make_xlsx([
            _row("SHP001", "Selesai", "T", sku_ref="S1", courier="Reguler (Cashless)-Ninja Xpress"),
        ])
        orders, _ = parse_shopee_xlsx(xlsx)
        assert orders[0].courier == "Ninja Xpress"

    def test_address_includes_city_and_province(self):
        xlsx = _make_xlsx([
            _row("SHP001", "Selesai", "T", sku_ref="S1",
                 address="******Jl. Test", city="KOTA BANDUNG", province="JAWA BARAT"),
        ])
        orders, _ = parse_shopee_xlsx(xlsx)
        addr = orders[0].address
        assert "KOTA BANDUNG" in addr
        assert "JAWA BARAT" in addr

    def test_raises_on_empty_file(self):
        xlsx = _make_xlsx([])
        with pytest.raises(ChannelImportError, match="kosong"):
            parse_shopee_xlsx(xlsx)

    def test_raises_on_missing_required_columns(self):
        # Build XLSX with only one column
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Kolom Tidak Dikenal"])
        ws.append(["data"])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        with pytest.raises(ChannelImportError, match="Kolom wajib tidak ditemukan"):
            parse_shopee_xlsx(buf)

    def test_raises_on_non_xlsx_bytes(self):
        buf = io.BytesIO(b"this is not excel")
        with pytest.raises(ChannelImportError, match="tidak dapat dibaca"):
            parse_shopee_xlsx(buf)

    def test_multiple_cancelled_same_order_id_is_one_order(self):
        """A multi-row cancelled order is one order with every line, not N orders."""
        xlsx = _make_xlsx([
            _row("SHP001", "Batal", "Produk A", sku_ref="A"),
            _row("SHP001", "Batal", "Produk B", sku_ref="B"),
        ])
        orders, skipped = parse_shopee_xlsx(xlsx)
        assert skipped == 0
        assert len(orders) == 1 and len(orders[0].lines) == 2

    def test_real_shopee_export_file(self):
        """Smoke test against the actual Shopee export sample in tests/fixtures/."""
        import os
        sample = "tests/fixtures/Order.all.20251201_20251231.xlsx"
        if not os.path.exists(sample):
            pytest.skip("Sample file not present")
        with open(sample, "rb") as f:
            orders, skipped = parse_shopee_xlsx(f)
        # 5 rows total: 2 Batal + 3 Selesai, all kept
        assert skipped == 0
        assert len(orders) == 5
        for o in orders:
            assert o.external_id
            assert len(o.lines) >= 1
