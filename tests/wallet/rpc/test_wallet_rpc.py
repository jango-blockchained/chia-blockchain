import dataclasses
import logging
from operator import attrgetter
from typing import Dict, Optional

import pytest
import pytest_asyncio
from blspy import G2Element

from chia.consensus.block_rewards import calculate_base_farmer_reward, calculate_pool_reward
from chia.consensus.coinbase import create_puzzlehash_for_pk
from chia.rpc.full_node_rpc_api import FullNodeRpcApi
from chia.rpc.full_node_rpc_client import FullNodeRpcClient
from chia.rpc.rpc_server import start_rpc_server
from chia.rpc.wallet_rpc_api import WalletRpcApi
from chia.rpc.wallet_rpc_client import WalletRpcClient
from chia.server.server import ChiaServer
from chia.simulator.full_node_simulator import FullNodeSimulator
from chia.simulator.simulator_protocol import FarmNewBlockProtocol
from chia.types.announcement import Announcement
from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.coin_record import CoinRecord
from chia.types.coin_spend import CoinSpend
from chia.types.peer_info import PeerInfo
from chia.types.spend_bundle import SpendBundle
from chia.util.bech32m import decode_puzzle_hash, encode_puzzle_hash
from chia.util.config import lock_and_load_config, save_config
from chia.util.hash import std_hash
from chia.util.ints import uint16, uint32, uint64
from chia.wallet.cat_wallet.cat_constants import DEFAULT_CATS
from chia.wallet.cat_wallet.cat_wallet import CATWallet
from chia.wallet.derive_keys import master_sk_to_wallet_sk, master_sk_to_wallet_sk_unhardened
from chia.wallet.trading.trade_status import TradeStatus
from chia.wallet.transaction_record import TransactionRecord
from chia.wallet.transaction_sorting import SortKey
from chia.wallet.util.compute_memos import compute_memos
from chia.wallet.util.wallet_types import WalletType
from chia.wallet.wallet import Wallet
from chia.wallet.wallet_node import WalletNode
from tests.block_tools import BlockTools
from tests.pools.test_pool_rpc import wallet_is_synced
from tests.time_out_assert import time_out_assert
from tests.util.socket import find_available_listen_port

log = logging.getLogger(__name__)


@dataclasses.dataclass
class WalletBundle:
    node: WalletNode
    rpc_client: WalletRpcClient
    wallet: Wallet


@dataclasses.dataclass
class FullNodeBundle:
    server: ChiaServer
    api: FullNodeSimulator
    rpc_client: FullNodeRpcClient


@dataclasses.dataclass
class WalletRpcTestEnvironment:
    wallet_1: WalletBundle
    wallet_2: WalletBundle
    full_node: FullNodeBundle


async def farm_transaction_block(full_node_api: FullNodeSimulator, wallet_node: WalletNode):
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(bytes32(b"\00" * 32)))
    await time_out_assert(5, wallet_is_synced, True, wallet_node, full_node_api)


async def farm_transaction(full_node_api: FullNodeSimulator, wallet_node: WalletNode, spend_bundle: SpendBundle):
    await time_out_assert(5, full_node_api.full_node.mempool_manager.get_spendbundle, spend_bundle, spend_bundle.name())
    await farm_transaction_block(full_node_api, wallet_node)
    assert full_node_api.full_node.mempool_manager.get_spendbundle(spend_bundle.name()) is None


async def generate_funds(full_node_api: FullNodeSimulator, wallet_bundle: WalletBundle, num_blocks: int = 5):
    wallet_id = 1
    initial_balances = await wallet_bundle.rpc_client.get_wallet_balance(str(wallet_id))
    ph: bytes32 = decode_puzzle_hash(await wallet_bundle.rpc_client.get_next_address(str(wallet_id), True))
    generated_funds = 0
    for i in range(0, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
        peak_height = full_node_api.full_node.blockchain.get_peak_height()
        assert peak_height is not None
        generated_funds += calculate_pool_reward(peak_height) + calculate_base_farmer_reward(peak_height)

    # Farm a dummy block to confirm the created funds
    await farm_transaction_block(full_node_api, wallet_bundle.node)

    expected_confirmed = initial_balances["confirmed_wallet_balance"] + generated_funds
    expected_unconfirmed = initial_balances["unconfirmed_wallet_balance"] + generated_funds
    await time_out_assert(10, get_confirmed_balance, expected_confirmed, wallet_bundle.rpc_client, wallet_id)
    await time_out_assert(10, get_unconfirmed_balance, expected_unconfirmed, wallet_bundle.rpc_client, wallet_id)
    await time_out_assert(10, wallet_bundle.rpc_client.get_synced)

    return generated_funds


@pytest_asyncio.fixture(scope="function", params=[True, False])
async def wallet_rpc_environment(two_wallet_nodes, request, bt: BlockTools, self_hostname):
    test_rpc_port: uint16 = uint16(find_available_listen_port())
    test_rpc_port_2: uint16 = uint16(find_available_listen_port())
    test_rpc_port_node: uint16 = uint16(find_available_listen_port())
    full_node, wallets = two_wallet_nodes
    full_node_api = full_node[0]
    full_node_server = full_node_api.full_node.server
    wallet_node, server_2 = wallets[0]
    wallet_node_2, server_3 = wallets[1]
    wallet = wallet_node.wallet_state_manager.main_wallet
    wallet_2 = wallet_node_2.wallet_state_manager.main_wallet

    wallet_rpc_api = WalletRpcApi(wallet_node)
    wallet_rpc_api_2 = WalletRpcApi(wallet_node_2)

    config = bt.config
    hostname = config["self_hostname"]
    daemon_port = config["daemon_port"]

    if request.param:
        wallet_node.config["trusted_peers"] = {full_node_server.node_id.hex(): full_node_server.node_id.hex()}
        wallet_node_2.config["trusted_peers"] = {full_node_server.node_id.hex(): full_node_server.node_id.hex()}
    else:
        wallet_node.config["trusted_peers"] = {}
        wallet_node_2.config["trusted_peers"] = {}

    def stop_node_cb():
        pass

    full_node_rpc_api = FullNodeRpcApi(full_node_api.full_node)

    rpc_cleanup_node = await start_rpc_server(
        full_node_rpc_api,
        hostname,
        daemon_port,
        test_rpc_port_node,
        stop_node_cb,
        bt.root_path,
        config,
        connect_to_daemon=False,
    )
    rpc_cleanup = await start_rpc_server(
        wallet_rpc_api,
        hostname,
        daemon_port,
        test_rpc_port,
        stop_node_cb,
        bt.root_path,
        config,
        connect_to_daemon=False,
    )
    rpc_cleanup_2 = await start_rpc_server(
        wallet_rpc_api_2,
        hostname,
        daemon_port,
        test_rpc_port_2,
        stop_node_cb,
        bt.root_path,
        config,
        connect_to_daemon=False,
    )

    await server_2.start_client(PeerInfo(self_hostname, uint16(full_node_server._port)), None)
    await server_3.start_client(PeerInfo(self_hostname, uint16(full_node_server._port)), None)

    client = await WalletRpcClient.create(hostname, test_rpc_port, bt.root_path, config)
    client_2 = await WalletRpcClient.create(hostname, test_rpc_port_2, bt.root_path, config)
    client_node = await FullNodeRpcClient.create(hostname, test_rpc_port_node, bt.root_path, config)

    wallet_bundle_1: WalletBundle = WalletBundle(wallet_node, client, wallet)
    wallet_bundle_2: WalletBundle = WalletBundle(wallet_node_2, client_2, wallet_2)
    node_bundle: FullNodeBundle = FullNodeBundle(full_node_server, full_node_api, client_node)

    yield WalletRpcTestEnvironment(wallet_bundle_1, wallet_bundle_2, node_bundle)

    # Checks that the RPC manages to stop the node
    client.close()
    client_2.close()
    client_node.close()
    await client.await_closed()
    await client_2.await_closed()
    await client_node.await_closed()
    await rpc_cleanup()
    await rpc_cleanup_2()
    await rpc_cleanup_node()


async def assert_wallet_types(client: WalletRpcClient, expected: Dict[WalletType, int]) -> None:
    for wallet_type in WalletType:
        wallets = await client.get_wallets(wallet_type)
        wallet_count = len(wallets)
        if wallet_type in expected:
            assert wallet_count == expected.get(wallet_type, 0)
            for wallet in wallets:
                assert wallet["type"] == wallet_type.value


async def tx_in_mempool(client: WalletRpcClient, transaction_id: bytes32):
    tx = await client.get_transaction("1", transaction_id)
    return tx.is_in_mempool()


async def get_confirmed_balance(client: WalletRpcClient, wallet_id: int):
    return (await client.get_wallet_balance(str(wallet_id)))["confirmed_wallet_balance"]


async def get_unconfirmed_balance(client: WalletRpcClient, wallet_id: int):
    return (await client.get_wallet_balance(str(wallet_id)))["unconfirmed_wallet_balance"]


@pytest.mark.asyncio
async def test_wallet_rpc(wallet_rpc_environment: WalletRpcTestEnvironment):
    env: WalletRpcTestEnvironment = wallet_rpc_environment

    wallet: Wallet = env.wallet_1.wallet
    wallet_2: Wallet = env.wallet_2.wallet

    wallet_node: WalletNode = env.wallet_1.node

    full_node_api: FullNodeSimulator = env.full_node.api

    client: WalletRpcClient = env.wallet_1.rpc_client
    client_2: WalletRpcClient = env.wallet_2.rpc_client
    client_node: FullNodeRpcClient = env.full_node.rpc_client

    generated_funds = await generate_funds(full_node_api, env.wallet_1)

    ph_2 = await wallet_2.get_new_puzzlehash()
    addr = encode_puzzle_hash(await wallet_2.get_new_puzzlehash(), "txch")
    tx_amount = uint64(15600000)
    with pytest.raises(ValueError):
        await client.send_transaction("1", uint64(100000000000000001), addr)

    # Tests sending a basic transaction
    tx = await client.send_transaction("1", tx_amount, addr, memos=["this is a basic tx"])
    transaction_id = tx.name

    spend_bundle = tx.spend_bundle
    assert spend_bundle is not None

    await time_out_assert(5, tx_in_mempool, True, client, transaction_id)
    await time_out_assert(5, get_unconfirmed_balance, generated_funds - tx_amount, client, 1)

    await farm_transaction(full_node_api, wallet_node, spend_bundle)

    # Checks that the memo can be retrieved
    tx_confirmed = await client.get_transaction("1", transaction_id)
    assert tx_confirmed.confirmed
    assert len(tx_confirmed.get_memos()) == 1
    assert [b"this is a basic tx"] in tx_confirmed.get_memos().values()
    assert list(tx_confirmed.get_memos().keys())[0] in [a.name() for a in spend_bundle.additions()]

    await time_out_assert(5, get_confirmed_balance, generated_funds - tx_amount, client, 1)

    # Tests offline signing
    ph_3 = await wallet_2.get_new_puzzlehash()
    ph_4 = await wallet_2.get_new_puzzlehash()
    ph_5 = await wallet_2.get_new_puzzlehash()

    # Test basic transaction to one output and coin announcement
    signed_tx_amount = 888000
    tx_coin_announcements = [
        Announcement(
            std_hash(b"coin_id_1"),
            std_hash(b"message"),
            b"\xca",
        ),
        Announcement(
            std_hash(b"coin_id_2"),
            bytes(Program.to("a string")),
        ),
    ]
    tx_res: TransactionRecord = await client.create_signed_transaction(
        [{"amount": signed_tx_amount, "puzzle_hash": ph_3}], coin_announcements=tx_coin_announcements
    )
    spend_bundle = tx_res.spend_bundle
    assert spend_bundle is not None

    assert tx_res.fee_amount == 0
    assert tx_res.amount == signed_tx_amount
    assert len(tx_res.additions) == 2  # The output and the change
    assert any([addition.amount == signed_tx_amount for addition in tx_res.additions])
    # check error for a ASSERT_ANNOUNCE_CONSUMED_FAILED and if the error is not there throw a value error
    try:
        push_res = await client_node.push_tx(spend_bundle)
    except ValueError as error:
        error_string = error.args[0]["error"]  # noqa:  # pylint: disable=E1126
        if error_string.find("ASSERT_ANNOUNCE_CONSUMED_FAILED") == -1:
            raise ValueError from error

    # # Test basic transaction to one output and puzzle announcement
    signed_tx_amount = 888000
    tx_puzzle_announcements = [
        Announcement(
            std_hash(b"puzzle_hash_1"),
            b"message",
            b"\xca",
        ),
        Announcement(
            std_hash(b"puzzle_hash_2"),
            bytes(Program.to("a string")),
        ),
    ]
    tx_res = await client.create_signed_transaction(
        [{"amount": signed_tx_amount, "puzzle_hash": ph_3}], puzzle_announcements=tx_puzzle_announcements
    )
    spend_bundle = tx_res.spend_bundle
    assert spend_bundle is not None

    assert tx_res.fee_amount == 0
    assert tx_res.amount == signed_tx_amount
    assert len(tx_res.additions) == 2  # The output and the change
    assert any([addition.amount == signed_tx_amount for addition in tx_res.additions])
    # check error for a ASSERT_ANNOUNCE_CONSUMED_FAILED and if the error is not there throw a value error
    try:
        push_res = await client_node.push_tx(spend_bundle)
    except ValueError as error:
        error_string = error.args[0]["error"]  # noqa:  # pylint: disable=E1126
        if error_string.find("ASSERT_ANNOUNCE_CONSUMED_FAILED") == -1:
            raise ValueError from error

    # Test basic transaction to one output
    signed_tx_amount = 888000
    tx_res = await client.create_signed_transaction(
        [{"amount": signed_tx_amount, "puzzle_hash": ph_3, "memos": ["My memo"]}]
    )

    assert tx_res.fee_amount == 0
    assert tx_res.amount == signed_tx_amount
    assert len(tx_res.additions) == 2  # The output and the change
    assert any([addition.amount == signed_tx_amount for addition in tx_res.additions])

    spend_bundle = tx_res.spend_bundle
    assert spend_bundle is not None

    push_res = await client.push_tx(spend_bundle)
    assert push_res["success"]
    assert await get_confirmed_balance(client, 1) == generated_funds - tx_amount

    await farm_transaction(full_node_api, wallet_node, spend_bundle)

    await time_out_assert(5, get_confirmed_balance, generated_funds - tx_amount - signed_tx_amount, client, 1)

    # Test transaction to two outputs, from a specified coin, with a fee
    coin_to_spend = None
    for addition in tx_res.additions:
        if addition.amount != signed_tx_amount:
            coin_to_spend = addition
    assert coin_to_spend is not None

    tx_res = await client.create_signed_transaction(
        [{"amount": 444, "puzzle_hash": ph_4, "memos": ["hhh"]}, {"amount": 999, "puzzle_hash": ph_5}],
        coins=[coin_to_spend],
        fee=uint64(100),
    )
    spend_bundle = tx_res.spend_bundle
    assert spend_bundle is not None

    assert tx_res.fee_amount == 100
    assert tx_res.amount == 444 + 999
    assert len(tx_res.additions) == 3  # The outputs and the change
    assert any([addition.amount == 444 for addition in tx_res.additions])
    assert any([addition.amount == 999 for addition in tx_res.additions])
    assert sum([rem.amount for rem in tx_res.removals]) - sum([ad.amount for ad in tx_res.additions]) == 100

    push_res = await client_node.push_tx(spend_bundle)
    assert push_res["success"]

    await farm_transaction(full_node_api, wallet_node, spend_bundle)

    found: bool = False
    for addition in spend_bundle.additions():
        if addition.amount == 444:
            cr: Optional[CoinRecord] = await client_node.get_coin_record_by_name(addition.name())
            assert cr is not None
            spend: Optional[CoinSpend] = await client_node.get_puzzle_and_solution(
                addition.parent_coin_info, cr.confirmed_block_index
            )
            assert spend is not None
            sb: SpendBundle = SpendBundle([spend], G2Element())
            assert compute_memos(sb) == {addition.name(): [b"hhh"]}
            found = True
    assert found

    new_balance = generated_funds - tx_amount - signed_tx_amount - 444 - 999 - 100
    await time_out_assert(5, get_confirmed_balance, new_balance, client, 1)

    send_tx_res: TransactionRecord = await client.send_transaction_multi(
        "1",
        [
            {"amount": 555, "puzzle_hash": ph_4, "memos": ["FiMemo"]},
            {"amount": 666, "puzzle_hash": ph_5, "memos": ["SeMemo"]},
        ],
        fee=uint64(200),
    )
    spend_bundle = send_tx_res.spend_bundle
    assert spend_bundle is not None
    assert send_tx_res is not None
    assert send_tx_res.fee_amount == 200
    assert send_tx_res.amount == 555 + 666
    assert len(send_tx_res.additions) == 3  # The outputs and the change
    assert any([addition.amount == 555 for addition in send_tx_res.additions])
    assert any([addition.amount == 666 for addition in send_tx_res.additions])
    assert sum([rem.amount for rem in send_tx_res.removals]) - sum([ad.amount for ad in send_tx_res.additions]) == 200

    await farm_transaction(full_node_api, wallet_node, spend_bundle)

    new_balance = new_balance - 555 - 666 - 200
    await time_out_assert(5, get_confirmed_balance, new_balance, client, 1)

    address = await client.get_next_address("1", True)
    assert len(address) > 10

    transactions = await client.get_transactions("1")
    assert len(transactions) > 1

    all_transactions = await client.get_transactions("1")
    # Test transaction pagination
    some_transactions = await client.get_transactions("1", 0, 5)
    some_transactions_2 = await client.get_transactions("1", 5, 10)
    assert some_transactions == all_transactions[0:5]
    assert some_transactions_2 == all_transactions[5:10]

    # Testing sorts
    # Test the default sort (CONFIRMED_AT_HEIGHT)
    assert all_transactions == sorted(all_transactions, key=attrgetter("confirmed_at_height"))
    all_transactions = await client.get_transactions("1", reverse=True)
    assert all_transactions == sorted(all_transactions, key=attrgetter("confirmed_at_height"), reverse=True)

    # Test RELEVANCE
    await client.send_transaction("1", uint64(1), encode_puzzle_hash(ph_2, "txch"))  # Create a pending tx

    all_transactions = await client.get_transactions("1", sort_key=SortKey.RELEVANCE)
    sorted_transactions = sorted(all_transactions, key=attrgetter("created_at_time"), reverse=True)
    sorted_transactions = sorted(sorted_transactions, key=attrgetter("confirmed_at_height"), reverse=True)
    sorted_transactions = sorted(sorted_transactions, key=attrgetter("confirmed"))
    assert all_transactions == sorted_transactions

    all_transactions = await client.get_transactions("1", sort_key=SortKey.RELEVANCE, reverse=True)
    sorted_transactions = sorted(all_transactions, key=attrgetter("created_at_time"))
    sorted_transactions = sorted(sorted_transactions, key=attrgetter("confirmed_at_height"))
    sorted_transactions = sorted(sorted_transactions, key=attrgetter("confirmed"), reverse=True)
    assert all_transactions == sorted_transactions

    # Checks that the memo can be retrieved
    tx_confirmed = await client.get_transaction("1", send_tx_res.name)
    assert tx_confirmed.confirmed
    if isinstance(tx_confirmed, SpendBundle):
        memos = compute_memos(tx_confirmed)
    else:
        memos = tx_confirmed.get_memos()
    assert len(memos) == 2
    print(memos)
    assert [b"FiMemo"] in memos.values()
    assert [b"SeMemo"] in memos.values()
    spend_bundle = send_tx_res.spend_bundle
    assert spend_bundle is not None
    assert list(memos.keys())[0] in [a.name() for a in spend_bundle.additions()]
    assert list(memos.keys())[1] in [a.name() for a in spend_bundle.additions()]

    # Test get_transactions to address
    ph_by_addr = await wallet.get_new_puzzlehash()
    await client.send_transaction("1", uint64(1), encode_puzzle_hash(ph_by_addr, "txch"))
    await client.farm_block(encode_puzzle_hash(ph_by_addr, "txch"))
    await time_out_assert(10, wallet_is_synced, True, wallet_node, full_node_api)
    tx_for_address = await client.get_transactions("1", to_address=encode_puzzle_hash(ph_by_addr, "txch"))
    assert len(tx_for_address) == 1
    assert tx_for_address[0].to_puzzle_hash == ph_by_addr

    # Test coin selection
    selected_coins = await client.select_coins(amount=1, wallet_id=1)
    assert len(selected_coins) > 0

    ##############
    # CATS       #
    ##############

    # Creates a CAT wallet with 100 mojos and a CAT with 20 mojos
    await client.create_new_cat_and_wallet(uint64(100))
    res = await client.create_new_cat_and_wallet(uint64(20))
    assert res["success"]
    cat_0_id = res["wallet_id"]
    asset_id = bytes32.fromhex(res["asset_id"])
    assert len(asset_id) > 0

    await assert_wallet_types(client, {WalletType.STANDARD_WALLET: 1, WalletType.CAT: 2})
    await assert_wallet_types(client_2, {WalletType.STANDARD_WALLET: 1})

    bal_0 = await client.get_wallet_balance(cat_0_id)
    assert bal_0["confirmed_wallet_balance"] == 0
    assert bal_0["pending_coin_removal_count"] == 1
    col = await client.get_cat_asset_id(cat_0_id)
    assert col == asset_id
    assert (await client.get_cat_name(cat_0_id)) == CATWallet.default_wallet_name_for_unknown_cat(asset_id.hex())
    await client.set_cat_name(cat_0_id, "My cat")
    assert (await client.get_cat_name(cat_0_id)) == "My cat"
    result = await client.cat_asset_id_to_name(col)
    assert result is not None
    wid, name = result
    assert wid == cat_0_id
    assert name == "My cat"
    result = await client.cat_asset_id_to_name(bytes32([0] * 32))
    assert result is None
    verified_asset_id = next(iter(DEFAULT_CATS.items()))[1]["asset_id"]
    result = await client.cat_asset_id_to_name(bytes32.from_hexstr(verified_asset_id))
    assert result is not None
    should_be_none, name = result
    assert should_be_none is None
    assert name == next(iter(DEFAULT_CATS.items()))[1]["name"]

    await farm_transaction_block(full_node_api, wallet_node)

    await time_out_assert(10, get_confirmed_balance, 20, client, cat_0_id)
    bal_0 = await client.get_wallet_balance(cat_0_id)
    assert bal_0["pending_coin_removal_count"] == 0
    assert bal_0["unspent_coin_count"] == 1

    # Creates a second wallet with the same CAT
    res = await client_2.create_wallet_for_existing_cat(asset_id)
    assert res["success"]
    cat_1_id = res["wallet_id"]
    cat_1_asset_id = bytes.fromhex(res["asset_id"])
    assert cat_1_asset_id == asset_id

    await assert_wallet_types(client, {WalletType.STANDARD_WALLET: 1, WalletType.CAT: 2})
    await assert_wallet_types(client_2, {WalletType.STANDARD_WALLET: 1, WalletType.CAT: 1})

    await farm_transaction_block(full_node_api, wallet_node)

    bal_1 = await client_2.get_wallet_balance(cat_1_id)
    assert bal_1["confirmed_wallet_balance"] == 0

    addr_0 = await client.get_next_address(cat_0_id, False)
    addr_1 = await client_2.get_next_address(cat_1_id, False)

    assert addr_0 != addr_1

    tx_res = await client.cat_spend(cat_0_id, uint64(4), addr_1, uint64(0), ["the cat memo"])
    spend_bundle = tx_res.spend_bundle
    assert spend_bundle is not None
    await farm_transaction(full_node_api, wallet_node, spend_bundle)

    # Test unacknowledged CAT
    assert wallet_node.wallet_state_manager is not None
    await wallet_node.wallet_state_manager.interested_store.add_unacknowledged_token(
        asset_id, "Unknown", uint32(10000), bytes32(b"\00" * 32)
    )
    cats = await client.get_stray_cats()
    assert len(cats) == 1

    await time_out_assert(10, get_confirmed_balance, 16, client, cat_0_id)
    await time_out_assert(10, get_confirmed_balance, 4, client_2, cat_1_id)

    # Test CAT coin selection
    selected_coins = await client.select_coins(amount=1, wallet_id=cat_0_id)
    assert len(selected_coins) > 0

    ##########
    # Offers #
    ##########

    # Create an offer of 5 chia for one CAT
    offer, trade_record = await client.create_offer_for_ids({uint32(1): -5, cat_0_id: 1}, validate_only=True)
    all_offers = await client.get_all_offers()
    assert len(all_offers) == 0
    assert offer is None

    offer, trade_record = await client.create_offer_for_ids({uint32(1): -5, cat_0_id: 1}, fee=uint64(1))
    assert offer is not None

    summary = await client.get_offer_summary(offer)
    assert summary == {"offered": {"xch": 5}, "requested": {col.hex(): 1}, "fees": 1}

    assert await client.check_offer_validity(offer)

    all_offers = await client.get_all_offers(file_contents=True)
    assert len(all_offers) == 1
    assert TradeStatus(all_offers[0].status) == TradeStatus.PENDING_ACCEPT
    assert all_offers[0].offer == bytes(offer)

    trade_record = await client_2.take_offer(offer, fee=uint64(1))
    assert TradeStatus(trade_record.status) == TradeStatus.PENDING_CONFIRM

    await client.cancel_offer(offer.name(), secure=False)

    trade_record = await client.get_offer(offer.name(), file_contents=True)
    assert trade_record.offer == bytes(offer)
    assert TradeStatus(trade_record.status) == TradeStatus.CANCELLED

    await client.cancel_offer(offer.name(), fee=uint64(1), secure=True)

    trade_record = await client.get_offer(offer.name())
    assert TradeStatus(trade_record.status) == TradeStatus.PENDING_CANCEL

    new_offer, new_trade_record = await client.create_offer_for_ids({uint32(1): -5, cat_0_id: 1}, fee=uint64(1))
    all_offers = await client.get_all_offers()
    assert len(all_offers) == 2

    await farm_transaction_block(full_node_api, wallet_node)

    async def is_trade_confirmed(client, trade) -> bool:
        trade_record = await client.get_offer(trade.name())
        return TradeStatus(trade_record.status) == TradeStatus.CONFIRMED

    await time_out_assert(15, is_trade_confirmed, True, client, offer)

    # Test trade sorting
    def only_ids(trades):
        return [t.trade_id for t in trades]

    trade_record = await client.get_offer(offer.name())
    all_offers = await client.get_all_offers(include_completed=True)  # confirmed at index descending
    assert len(all_offers) == 2
    assert only_ids(all_offers) == only_ids([trade_record, new_trade_record])
    all_offers = await client.get_all_offers(include_completed=True, reverse=True)  # confirmed at index ascending
    assert only_ids(all_offers) == only_ids([new_trade_record, trade_record])
    all_offers = await client.get_all_offers(include_completed=True, sort_key="RELEVANCE")  # most relevant
    assert only_ids(all_offers) == only_ids([new_trade_record, trade_record])
    all_offers = await client.get_all_offers(
        include_completed=True, sort_key="RELEVANCE", reverse=True
    )  # least relevant
    assert only_ids(all_offers) == only_ids([trade_record, new_trade_record])
    # Test pagination
    all_offers = await client.get_all_offers(include_completed=True, start=0, end=1)
    assert len(all_offers) == 1
    all_offers = await client.get_all_offers(include_completed=True, start=50)
    assert len(all_offers) == 0
    all_offers = await client.get_all_offers(include_completed=True, start=0, end=50)
    assert len(all_offers) == 2

    # Keys and addresses

    address = await client.get_next_address("1", True)
    assert len(address) > 10

    all_transactions = await client.get_transactions("1")

    some_transactions = await client.get_transactions("1", 0, 5)
    some_transactions_2 = await client.get_transactions("1", 5, 10)
    assert len(all_transactions) > 1
    assert some_transactions == all_transactions[0:5]
    assert some_transactions_2 == all_transactions[5:10]

    transaction_count = await client.get_transaction_count("1")
    assert transaction_count == len(all_transactions)

    pks = await client.get_public_keys()
    assert len(pks) == 1

    assert (await client.get_height_info()) > 0

    created_tx = await client.send_transaction("1", tx_amount, addr)

    await time_out_assert(5, tx_in_mempool, True, client, created_tx.name)
    assert len(await wallet.wallet_state_manager.tx_store.get_unconfirmed_for_wallet(1)) == 1
    await client.delete_unconfirmed_transactions("1")
    assert len(await wallet.wallet_state_manager.tx_store.get_unconfirmed_for_wallet(1)) == 0

    sk_dict = await client.get_private_key(pks[0])
    assert sk_dict["fingerprint"] == pks[0]
    assert sk_dict["sk"] is not None
    assert sk_dict["pk"] is not None
    assert sk_dict["seed"] is not None

    mnemonic = await client.generate_mnemonic()
    assert len(mnemonic) == 24

    await client.add_key(mnemonic)

    pks = await client.get_public_keys()
    assert len(pks) == 2

    await client.log_in(pks[1])
    sk_dict = await client.get_private_key(pks[1])
    assert sk_dict["fingerprint"] == pks[1]

    # Add in reward addresses into farmer and pool for testing delete key checks
    # set farmer to first private key
    sk = await wallet_node.get_key_for_fingerprint(pks[0])
    test_ph = create_puzzlehash_for_pk(master_sk_to_wallet_sk(sk, uint32(0)).get_g1())
    with lock_and_load_config(wallet_node.root_path, "config.yaml") as test_config:
        test_config["farmer"]["xch_target_address"] = encode_puzzle_hash(test_ph, "txch")
        # set pool to second private key
        sk = await wallet_node.get_key_for_fingerprint(pks[1])
        test_ph = create_puzzlehash_for_pk(master_sk_to_wallet_sk(sk, uint32(0)).get_g1())
        test_config["pool"]["xch_target_address"] = encode_puzzle_hash(test_ph, "txch")
        save_config(wallet_node.root_path, "config.yaml", test_config)

    # Check first key
    sk_dict = await client.check_delete_key(pks[0])
    assert sk_dict["fingerprint"] == pks[0]
    assert sk_dict["used_for_farmer_rewards"] is True
    assert sk_dict["used_for_pool_rewards"] is False

    # Check second key
    sk_dict = await client.check_delete_key(pks[1])
    assert sk_dict["fingerprint"] == pks[1]
    assert sk_dict["used_for_farmer_rewards"] is False
    assert sk_dict["used_for_pool_rewards"] is True

    # Check unknown key
    sk_dict = await client.check_delete_key(123456, 10)
    assert sk_dict["fingerprint"] == 123456
    assert sk_dict["used_for_farmer_rewards"] is False
    assert sk_dict["used_for_pool_rewards"] is False

    # Add in observer reward addresses into farmer and pool for testing delete key checks
    # set farmer to first private key
    sk = await wallet_node.get_key_for_fingerprint(pks[0])
    test_ph = create_puzzlehash_for_pk(master_sk_to_wallet_sk_unhardened(sk, uint32(0)).get_g1())
    with lock_and_load_config(wallet_node.root_path, "config.yaml") as test_config:
        test_config["farmer"]["xch_target_address"] = encode_puzzle_hash(test_ph, "txch")
        # set pool to second private key
        sk = await wallet_node.get_key_for_fingerprint(pks[1])
        test_ph = create_puzzlehash_for_pk(master_sk_to_wallet_sk_unhardened(sk, uint32(0)).get_g1())
        test_config["pool"]["xch_target_address"] = encode_puzzle_hash(test_ph, "txch")
        save_config(wallet_node.root_path, "config.yaml", test_config)

    # Check first key
    sk_dict = await client.check_delete_key(pks[0])
    assert sk_dict["fingerprint"] == pks[0]
    assert sk_dict["used_for_farmer_rewards"] is True
    assert sk_dict["used_for_pool_rewards"] is False

    # Check second key
    sk_dict = await client.check_delete_key(pks[1])
    assert sk_dict["fingerprint"] == pks[1]
    assert sk_dict["used_for_farmer_rewards"] is False
    assert sk_dict["used_for_pool_rewards"] is True

    # Check unknown key
    sk_dict = await client.check_delete_key(123456, 10)
    assert sk_dict["fingerprint"] == 123456
    assert sk_dict["used_for_farmer_rewards"] is False
    assert sk_dict["used_for_pool_rewards"] is False

    await client.delete_key(pks[0])
    await client.log_in(pks[1])
    assert len(await client.get_public_keys()) == 1

    assert not (await client.get_sync_status())

    wallets = await client.get_wallets()
    assert len(wallets) == 1
    assert await get_unconfirmed_balance(client, int(wallets[0]["id"])) == 0

    with pytest.raises(ValueError):
        await client.send_transaction(wallets[0]["id"], uint64(100), addr)

    # Delete all keys
    await client.delete_all_keys()
    assert len(await client.get_public_keys()) == 0
