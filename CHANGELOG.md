# Changelog

## 0.0.15 (2026-11-20)

Sales invoices tied to the books
- PPh withheld by the buyer on invoice payments and multi-invoice receipts (account 1520 PPh dibayar di muka),
  read automatically into the SPT Tahunan credits.
- Hapus piutang: write off an unpaid invoice against the receivable provision (1255) then 6970, with approval above
  the threshold; reversible.
- Selisih & klaim marketplace (`/books/payouts/selisih/`): charges and claims with Shopee, Tokopedia and others that
  are not in a payout file, settled later through the bank. New accounts 1290 Piutang lainnya and 2390 Utang lainnya.
- Books check (Sesuai / Selisih) on invoice, bill, credit note and cash transaction pages; new control listing each
  invoice whose Piutang differs from its open balance.
- Combined invoice (Gabungan) PDF, Mitra filter on the invoice list.
- Manual journal form keeps typed lines after an error, refuses double submits, takes an attachment, and can copy a
  journal.
- Beranda: "Siapkan pembukuan" checklist (tax profile, bank accounts, opening balances, first invoice).

Screens rebuilt in the shell style
- Customers, customer detail, Pesanan list, order detail and new order, mitra sales list and entry (mitra prices),
  Ringkasan penjualan (refunds now taken off; bars no longer lose their width under the Indonesian locale),
  payment link page.

Migrations: `tax/0014`, `accounting/0023`, `accounting/0024` (applied on start by the web container).
