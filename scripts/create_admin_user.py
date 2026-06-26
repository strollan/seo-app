import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.auth_agent import create_user, init_auth_db


def main():
    init_auth_db()

    username = input("Admin username/email: ").strip()
    password = getpass.getpass("Admin password, 12+ chars: ")
    confirm = getpass.getpass("Confirm password: ")

    if password != confirm:
        raise SystemExit("Passwords do not match.")

    created = create_user(username=username, password=password, role="admin")
    print(f"Created admin user: {created}")


if __name__ == "__main__":
    main()
