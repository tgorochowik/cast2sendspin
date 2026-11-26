"""Authorized source-only Sendspin client; no microphone/device capture."""
import asyncio
import os
from pathlib import Path

from aiosendspin.client.client import SendspinClient
from aiosendspin.models.core import DeviceInfo
from aiosendspin.models.source import ClientHelloSourceSupport
from aiosendspin.models.types import Roles
from aiosendspin.noise.keys import Identity, b64url_decode, generate_psk, psk_id_for
from aiosendspin.noise.pairing_token import PSKPairingToken, encode_psk_token
from aiosendspin.noise.trust_store import FileClientPairingStore, PairingPsk
from aiosendspin.noise.trust_store import PskCategory


async def connect_source(ma, config, on_command):
    state = Path(config.get("state_dir", ".state"))
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = state / "sendspin-key"
    if path.exists():
        identity = Identity.from_private_bytes(b64url_decode(path.read_text()))
    else:
        identity = Identity.generate()
        path.write_text(identity.private_b64u)
        path.chmod(0o600)
    store = await FileClientPairingStore.open(state / "sendspin-pairing.json")
    pairing = await store.pairing_psk()
    if pairing is None:
        key = generate_psk()
        pairing = PairingPsk(psk_id_for(key), key)
        await store.set_pairing_psk(pairing)
    client = SendspinClient(
        identity, config.get("source_name", "YouTube Music Cast Input"), [Roles.SOURCE],
        pairing_store=store,
        source_support=ClientHelloSourceSupport(),
        device_info=DeviceInfo(product_name="cast2sendspin", manufacturer="cast2sendspin"),
        session=ma.session,
    )
    client.add_server_command_listener(on_command)
    # Bootstrap on the explicitly configured LAN server. Persist its Sendspin
    # identity (different from MA's API server_id) after authenticated pairing.
    pin = state / "sendspin-server-id"
    if not pin.exists():
        await client.set_unpaired_access(enabled=True)
    await client.connect(
        config["sendspin_url"],
        expected_server_id=pin.read_text().strip() if pin.exists() else None,
    )
    try:
        # Wait for MA's asynchronous source-player registration.
        async with asyncio.timeout(15):
            while True:
                players = await ma.call("players/all")
                if any(p["player_id"] == identity.peer_id for p in players):
                    break
                await asyncio.sleep(0.2)
        # Set up this newly created source only; do not change existing players.
        flow = (
            {"type": "finish"}
            if client.noise_psk and client.noise_psk.category is PskCategory.LONG_TERM
            else await ma.call("config/players/setup", player_id=identity.peer_id)
        )
        if not isinstance(flow, dict):
            raise RuntimeError(f"Unexpected setup response shape: {type(flow).__name__}")
        token = encode_psk_token(PSKPairingToken(identity.peer_id, pairing.psk))
        for _ in range(15):
            step = flow.get("step_id")
            if flow.get("type") in ("finish", "abort"):
                if flow["type"] == "abort":
                    raise RuntimeError("Source setup aborted")
                break
            if step == "enter_token":
                values = {"pairing_token": token}
            elif step == "approve_device_source":
                values = {"connect_method": "pair"}
            elif step == "select_method":
                values = {"pairing_method": "token"}
            elif flow.get("type") == "progress":
                await asyncio.sleep(0.2)
                flow = await ma.call("config/flows/get", flow_id=flow["flow_id"])
                continue
            else:
                raise RuntimeError(f"Unexpected source setup step: {step}")
            flow = await ma.call("config/flows/submit", flow_id=flow["flow_id"], values=values)
        else:
            raise RuntimeError("Source pairing did not finish")
        await client.set_unpaired_access(enabled=False)
        pin.write_text(client.server_info.server_id)
        async with asyncio.timeout(15):
            while not client.is_time_synchronized():
                await asyncio.sleep(0.1)
        return client, identity.peer_id
    except BaseException:
        await client.disconnect()
        raise
