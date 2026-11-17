"""Test üçün imzalanmış şəxsiyyətlər (F-1). Açar real sirr deyil: sınaq üçün sabit törəmə; real açar yalnız server mühitindədir."""
import hashlib

from agents.factory_auth import PrincipalAuthority

AUTH = PrincipalAuthority(hashlib.sha256(b"naymera-test-authority-not-a-secret").digest())
OWNER = AUTH.owner("owner", "console")
SYSTEM = AUTH.system("naymera", "core")
DISPATCHER = AUTH.dispatcher("dispatcher", "runtime")
MOCK = AUTH.mock_owner("owner", "test")
STRANGER = AUTH.owner("mallory", "console")
