"""Install a client's store (partner only). Example:

    manage.py install_store --name "Toko Maju" --code toko-maju --owner "Budi Santoso" \\
        --email budi@tokomaju.co.id --phone 081234567890 --legal-name "PT Maju Jaya" \\
        --npwp 0123456789012345 --pkp

Add ``--organization <kode>`` to add a brand to a client that is already installed."""
from django.core.management.base import BaseCommand, CommandError

from apps.shops.models import Organization
from apps.shops.provisioning import InstallError, install


class Command(BaseCommand):
    help = "Install a store for a client: owner account, organization, store, warehouse, chart of accounts, tax profile."

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True, help="Store (brand) name")
        parser.add_argument("--code", required=True, help="Store code: 3-40 lowercase letters, digits or hyphens")
        parser.add_argument("--owner", required=True, help="Owner's full name")
        parser.add_argument("--email", default="", help="Owner's email (login)")
        parser.add_argument("--phone", default="", help="Owner's WhatsApp number (login when there is no email)")
        parser.add_argument("--legal-name", default="", help="Name on the NPWP; defaults to the store name")
        parser.add_argument("--npwp", default="")
        parser.add_argument("--address", default="")
        parser.add_argument("--pkp", action="store_true", help="The client is a PKP (charges PPN)")
        parser.add_argument("--individual", action="store_true", help="Orang pribadi instead of badan usaha")
        parser.add_argument("--organization", default="", help="Code of an existing organization, to add a brand")
        parser.add_argument("--plan", default="", help="Subscription plan code; none means no plan limits")

    def handle(self, *args, **options):
        from apps.billing.models import Plan
        organization = plan = None
        if options["organization"]:
            organization = Organization.objects.filter(slug=options["organization"]).first()
            if not organization:
                raise CommandError(f"Organisasi {options['organization']} tidak ditemukan.")
        if options["plan"]:
            plan = Plan.objects.filter(code=options["plan"]).first()
            if not plan:
                raise CommandError(f"Paket {options['plan']} tidak ditemukan.")
        try:
            result = install(store_name=options["name"], slug=options["code"], owner_name=options["owner"],
                owner_email=options["email"], owner_phone=options["phone"], legal_name=options["legal_name"],
                npwp=options["npwp"], address=options["address"], is_pkp=options["pkp"],
                taxpayer_type="individual" if options["individual"] else "company",
                organization=organization, plan=plan)
        except InstallError as error:
            raise CommandError(str(error)) from None
        store, owner = result["store"], result["owner"]
        self.stdout.write(self.style.SUCCESS(f"Toko {store.name} ({store.slug}) terpasang."))
        self.stdout.write(f"Organisasi : {store.organization.name} ({store.organization.slug})")
        self.stdout.write(f"Pemilik    : {owner.get_full_name() or owner.username} · masuk dengan {owner.username}")
        if result["invite_link"]:
            self.stdout.write(f"Link kata sandi (sekali pakai, berikan ke pemilik):\n  {result['invite_link']}")
        else:
            self.stdout.write("Pemilik sudah punya kata sandi dan masuk dengan akunnya.")
