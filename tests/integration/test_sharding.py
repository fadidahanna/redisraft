"""
This file is part of RedisRaft.

Copyright (c) 2020-2021 Redis Ltd.

RedisRaft is licensed under the Redis Source Available License (RSAL).
"""

import time
from redis import ResponseError
from pytest import raises
from .sandbox import assert_after


def test_invalid_shardgroup_replace(cluster):
    cluster.create(3, raft_args={'sharding': 'yes'})
    c = cluster.node(1).client

    # not enough entries 1 shard
    with raises(ResponseError, match="wrong number of arguments for 'raft.shardgroup"):
        c.execute_command(
            'RAFT.SHARDGROUP', 'REPLACE',
            '1',
            cluster.leader_node().info()['raft_dbid'],
            '1', '1',
            '0', '16383', '1',
            '1234567890123456789012345678901234567890', '2.2.2.2:2222',
        )

    # not enough entries 2 shard
    with raises(ResponseError, match="wrong number of arguments for 'raft.shardgroup"):
        c.execute_command(
            'RAFT.SHARDGROUP', 'REPLACE',
            '2',
            '12345678901234567890123456789012',
            '0', '1',
            '1234567890123456789012345678901234567890', '2.2.2.2:2222',
            cluster.leader_node().info()['raft_dbid'],
            '1', '1',
            '0', '16383', '1',
            '1234567890123456789012345678901234567890', '2.2.2.2:2222',
        )


def test_cross_slot_violation(cluster):
    cluster.create(3, raft_args={'sharding': 'yes'})
    c = cluster.node(1).client

    assert c.execute_command(
        'RAFT.SHARDGROUP', 'REPLACE',
        '2',
        '12345678901234567890123456789012',
        '0', '1',
        '1234567890123456789012345678901234567890', '2.2.2.2:2222',
        cluster.leader_node().info()['raft_dbid'],
        '1', '1',
        '0', '16383', '1', '0',
        '1234567890123456789012345678901234567890', '2.2.2.2:2222',
    ) == b'OK'

    # -CROSSSLOT on multi-key cross slot violation
    with raises(ResponseError, match='CROSSSLOT'):
        c.mset({'key1': 'val1', 'key2': 'val2'})

    # With tags, it should succeed
    assert c.mset({'{tag1}key1': 'val1', '{tag1}key2': 'val2'})

    # MULTI/EXEC with cross slot between commands
    txn = cluster.node(1).client.pipeline(transaction=True)
    txn.set('key1', 'val1')
    txn.set('key2', 'val2')
    with raises(ResponseError, match='CROSSSLOT'):
        txn.execute()

    # Wait followers just to be sure crossslot command does not cause a problem.
    cluster.wait_for_unanimity()


def test_shard_group_sanity(cluster):
    # Create a cluster with just a single slot
    cluster.create(3, raft_args={
        'sharding': 'yes',
        'slot-config': '0'})

    c = cluster.node(1).client

    # Operations on unmapped slots should fail
    with raises(ResponseError, match='CLUSTERDOWN'):
        c.set('key', 'value')

    # Add a fake shardgroup to get complete coverage
    assert c.execute_command(
        'RAFT.SHARDGROUP', 'ADD',
        '12345678901234567890123456789012',
        '1', '1',
        '1', '16382', '1', '0',
        '1234567890123456789012345678901234567890', '1.1.1.1:1111') == b'OK'
    with raises(ResponseError, match='MOVED [0-9]+ 1.1.1.1:111'):
        c.set('key', 'value')

    cluster.node(3).client.execute_command('CLUSTER', 'SLOTS')

    # Test by adding another fake shardgroup with same shardgroup id
    # should fail
    with raises(ResponseError, match='Invalid ShardGroup Update'):
        c.execute_command(
            'RAFT.SHARDGROUP', 'ADD',
            '12345678901234567890123456789012',
            '1', '1',
            '16383', '16383', '1', '0',
            '1234567890123456789012345678901234567890', '   1.1.1.1:1111')


def test_shard_group_replace(cluster):
    # Create a cluster with just a single slot
    cluster.create(3, raft_args={
        'sharding': 'yes',
        'slot-config': '0',
        'external-sharding': 'yes'})

    c = cluster.node(1).client

    # Tests wholesale replacement, while ignoring shardgroup
    # that corresponds to local sg
    assert c.execute_command(
        'RAFT.SHARDGROUP', 'REPLACE',
        '3',
        '12345678901234567890123456789012',
        '1', '1',
        '6', '7', '1', '0',
        '2' * 40, '2.2.2.2:2222',

        '12345678901234567890123456789013',
        '1', '1',
        '8', '16383', '1', '0',
        '3' * 40, '3.3.3.3:3333',

        cluster.leader_node().info()['raft_dbid'],
        '1', '1',
        '0', '5', '1', '0',
        '4' * 40, '2.2.2.2:2222',
    ) == b'OK'

    with raises(ResponseError, match='MOVED [0-9]+ 3.3.3.3:3333'):
        c.set('key', 'value')

    cluster.wait_for_unanimity()
    cluster.node(2).wait_for_log_applied()  # to get shardgroup

    def validate_slots(cluster_slots):
        assert len(cluster_slots) == 3
        leader = cluster.leader_node()
        local_id = "{}00000001".format(leader.info()['raft_dbid']).encode()

        for i in range(len(cluster_slots)):
            node_id = cluster_slots[i][2][2]
            start_slot = cluster_slots[i][0]
            end_slot = cluster_slots[i][1]

            if node_id == local_id:
                assert start_slot == 0, cluster_slots
                assert end_slot == 5, cluster_slots
            elif node_id == b'2' * 40:
                assert start_slot == 6, cluster_slots
                assert end_slot == 7, cluster_slots
            elif node_id == b'3' * 40:
                assert start_slot == 8, cluster_slots
                assert end_slot == 16383, cluster_slots
            else:
                assert False, "failed to match id {}".format(node_id)

    validate_slots(cluster.node(3).client.execute_command('CLUSTER', 'SLOTS'))

    def validate_nodes(cluster_nodes):
        assert len(cluster_nodes) == 5
        for i in [0, 1, 3, 4]:
            node = cluster_nodes[i].split(b' ')
            if node[0] == "{}00000001".format(
                    cluster.leader_node().info()['raft_dbid']).encode():
                assert node[2] == b"myself,master"
                assert node[3] == b"-"
                assert node[8] == b"0-5"
            elif node[0] == "{}00000002".format(
                    cluster.leader_node().info()['raft_dbid']).encode():
                assert node[2] == b"slave"
                assert node[3] == "{}00000001".format(
                    cluster.leader_node().info()['raft_dbid']).encode()
                assert node[8] == b"0-5"
            elif node[0] == "{}00000003".format(
                    cluster.leader_node().info()['raft_dbid']).encode():
                assert node[2] == b"slave"
                assert node[3] == "{}00000001".format(
                    cluster.leader_node().info()['raft_dbid']).encode()
                assert node[8] == b"0-5"
            elif node[0] == b'2' * 40:
                assert node[2] == b"master"
                assert node[3] == b"-"
                assert node[8] == b"6-7"
            elif node[0] == b'3' * 40:
                assert node[2] == b"master"
                assert node[3] == b"-"
                assert node[8] == b"8-16383"
            else:
                assert False, node

    validate_nodes(cluster.node(1).execute('CLUSTER', 'NODES').splitlines())


def test_shard_group_validation(cluster):
    cluster.create(3, raft_args={
        'sharding': 'yes',
        'slot-config': '0:1000'})

    c = cluster.node(1).client

    # Invalid range
    with raises(ResponseError, match='invalid'):
        c.execute_command(
            'RAFT.SHARDGROUP', 'ADD',
            '12345678901234567890123456789012',
            '1', '1',
            '1001', '20000', '1', '0',
            '1234567890123456789012345678901234567890', '1.1.1.1:1111')

    # Conflict
    with raises(ResponseError, match='invalid'):
        c.execute_command(
            'RAFT.SHARDGROUP', 'ADD',
            '12345678901234567890123456789012',
            '1', '1',
            '1000', '1001', '1', '0',
            '1234567890123456789012345678901234567890', '1.1.1.1:1111')


def test_shard_group_propagation(cluster):
    # Create a cluster with just a single slot
    cluster.create(3, raft_args={
        'sharding': 'yes',
        'slot-config': '0:1000'})

    c = cluster.node(1).client
    assert c.execute_command(
        'RAFT.SHARDGROUP', 'ADD',
        '1' * 32,
        '1', '1',
        '1001', '16383', '1', '0',
        '1234567890123456789012345678901234567890', '1.1.1.1:1111') == b'OK'

    cluster.wait_for_unanimity()
    cluster.node(3).wait_for_log_applied()

    cluster_slots = cluster.node(3).client.execute_command('CLUSTER', 'SLOTS')
    assert len(cluster_slots) == 2


def test_shard_group_snapshot_propagation(cluster):
    # Create a cluster with just a single slot
    cluster.create(1, raft_args={
        'sharding': 'yes',
        'slot-config': '0:1000'})

    c = cluster.node(1).client
    assert c.execute_command(
        'RAFT.SHARDGROUP', 'ADD',
        '12345678901234567890123456789012',
        '1', '1',
        '1001', '16383', '1', '0',
        '1234567890123456789012345678901234567890', '1.1.1.1:1111') == b'OK'

    assert c.execute_command('RAFT.DEBUG', 'COMPACT') == b'OK'

    # Add a new node. Since we don't have the shardgroup entries
    # in the log anymore, we'll rely on snapshot delivery.
    n2 = cluster.add_node(use_cluster_args=True)
    n2.wait_for_node_voting()

    assert (c.execute_command('CLUSTER', 'SLOTS') ==
            n2.client.execute_command('CLUSTER', 'SLOTS'))


def test_shard_group_persistence(cluster):
    cluster.create(1, raft_args={
        'sharding': 'yes',
        'slot-config': '0:1000',
        'external-sharding': 'yes',
    })

    n1 = cluster.node(1)
    assert n1.client.execute_command(
        'RAFT.SHARDGROUP', 'ADD',
        '12345678901234567890123456789012',
        '1', '1',
        '1001', '16383', '1', '0',
        '1234567890123456789012345678901234567890', '1.1.1.1:1111') == b'OK'

    # Make sure received cluster slots is sane
    def validate_slots(cluster_slots):
        assert len(cluster_slots) == 2
        local_id = "{}00000001".format(
            cluster.leader_node().info()['raft_dbid']).encode()
        for i in range(len(cluster_slots)):
            shargroup_id = cluster_slots[i][2][2]
            start_slot = cluster_slots[i][0]
            end_slot = cluster_slots[i][1]

            if shargroup_id == local_id:
                assert start_slot == 0, cluster_slots
                assert end_slot == 1000, cluster_slots
            elif shargroup_id == b"1234567890123456789012345678901234567890":
                assert start_slot == 1001, cluster_slots
                assert end_slot == 16383, cluster_slots
            else:
                assert False, "failed to match id {}".format(shargroup_id)

    validate_slots(n1.client.execute_command('CLUSTER', 'SLOTS'))

    # Restart and make sure cluster slots persisted
    n1.terminate()
    n1.start()
    n1.wait_for_node_voting()

    validate_slots(n1.client.execute_command('CLUSTER', 'SLOTS'))

    # Compact log, restart and make sure cluster slots persisted
    n1.client.execute_command('RAFT.DEBUG', 'COMPACT')

    n1.terminate()
    n1.start()
    n1.wait_for_node_voting()

    validate_slots(n1.client.execute_command('CLUSTER', 'SLOTS'))


def test_shard_group_linking(cluster_factory):
    cluster1 = cluster_factory().create(3, raft_args={
        'sharding': 'yes',
        'slot-config': '0:1',
        'shardgroup-update-interval': 500})
    cluster2 = cluster_factory().create(3, raft_args={
        'sharding': 'yes',
        'slot-config': '2:16383',
        'shardgroup-update-interval': 500})

    # Not expected to have coverage
    with raises(ResponseError, match='CLUSTERDOWN'):
        cluster1.node(1).client.set('key', 'value')

    # Link cluster1 -> cluster2
    assert cluster1.node(1).client.execute_command(
        'RAFT.SHARDGROUP', 'LINK',
        'localhost:%s' % cluster2.node(1).port) == b'OK'
    with raises(ResponseError, match='MOVED'):
        cluster1.node(1).client.set('key', 'value')

    # Test cluster2 -> cluster1 linking with redirect, i.e. provide a
    # non-leader address
    assert cluster2.node(1).client.execute_command(
        'RAFT.SHARDGROUP', 'LINK',
        'localhost:%s' % cluster1.node(2).port) == b'OK'

    # Verify CLUSTER SLOTS look good
    def validate_slots(cluster_slots):
        assert len(cluster_slots) == 2
        for i in range(len(cluster_slots)):
            if cluster_slots[i][2][1] == cluster1.leader_node().port:
                assert cluster_slots[i][0] == 0, cluster_slots
                assert cluster_slots[i][1] == 1, cluster_slots
            elif cluster_slots[i][2][1] == cluster2.leader_node().port:
                assert cluster_slots[i][0] == 2, cluster_slots
                assert cluster_slots[i][1] == 16383, cluster_slots
            else:
                assert False, "failed to match id {}".format(
                    cluster_slots[i][2][1])

    validate_slots(cluster1.node(1).client.execute_command('CLUSTER', 'SLOTS'))

    # Verify CLUSTER NODES looks good
    def validate_nodes(cluster_nodes):
        assert len(cluster_nodes) == 6
        for i in [0, 1, 3, 4]:
            node = cluster_nodes[i].split(b' ')
            if node[0] == "{}00000001".format(
                    cluster1.leader_node().info()['raft_dbid']).encode():
                assert node[2] == b"myself,master"
                assert node[3] == b"-"
                assert node[8] == b"0-1"
            elif node[0] == "{}00000002".format(
                    cluster1.leader_node().info()['raft_dbid']).encode():
                assert node[2] == b"slave"
                assert node[3] == "{}00000001".format(
                    cluster1.leader_node().info()['raft_dbid']).encode()
                assert node[8] == b"0-1"
            elif node[0] == "{}00000001".format(
                    cluster2.leader_node().info()['raft_dbid']).encode():
                assert node[2] == b"master"
                assert node[3] == b"-"
                assert node[8] == b"2-16383"
            elif node[0] == "{}00000002".format(
                    cluster2.leader_node().info()['raft_dbid']).encode():
                assert node[2] == b"slave"
                assert node[3] == b"-"
                assert node[8] == b"2-16383"
            else:
                assert False, node

    validate_nodes(cluster1.node(1).execute('CLUSTER', 'NODES').splitlines())

    # Terminate leader on cluster 2, wait for re-election and confirm
    # propagation.
    assert cluster2.leader == 1
    cluster2.leader_node().terminate()
    cluster2.update_leader()

    # Wait for shardgroup update interval, 500ms
    time.sleep(2)
    validate_slots(cluster1.node(1).client.execute_command('CLUSTER', 'SLOTS'))


def test_shard_group_linking_checks(cluster_factory):
    # Create clusters with overlapping hash slots,
    # linking should fail.
    cluster1 = cluster_factory().create(3, raft_args={
        'sharding': 'yes',
        'slot-config': '0:1'})
    cluster2 = cluster_factory().create(3, raft_args={
        'sharding': 'yes',
        'slot-config': '1:16383'})

    # Link
    with raises(ResponseError, match='failed to connect to cluster for link'):
        cluster1.node(1).client.execute_command(
            'RAFT.SHARDGROUP', 'LINK',
            'localhost:%s' % cluster2.node(1).port)


def test_shard_group_refresh(cluster_factory):
    # Confirm that topology changes are eventually propagated through the
    # shardgroup refresh mechanism.

    cluster1 = cluster_factory().create(3, raft_args={
        'sharding': 'yes',
        'slot-config': '0:8191'})
    cluster2 = cluster_factory().create(3, raft_args={
        'sharding': 'yes',
        'slot-config': '8192:16383'})

    assert cluster1.node(1).client.execute_command(
        'RAFT.SHARDGROUP', 'LINK',
        'localhost:%s' % cluster2.node(1).port) == b'OK'
    assert cluster2.node(1).client.execute_command(
        'RAFT.SHARDGROUP', 'LINK',
        'localhost:%s' % cluster1.node(1).port) == b'OK'

    # Sanity: confirm initial shardgroup was propagated
    def validate_slots(slots):
        assert len(slots) == 2
        for i in range(2):
            if slots[i][2][1] < 5100:
                assert slots[i][0:2] == [0, 8191], slots
                assert slots[i][2][0] == b'localhost', slots
                assert slots[i][2][1] == 5001, slots

    validate_slots(cluster2.node(1).client.execute_command('CLUSTER', 'SLOTS'))

    # Terminate cluster1/node1 and wait for election
    cluster1.node(1).terminate()
    time.sleep(2)
    cluster1.node(2).wait_for_election()

    # Make sure the new leader is propagated to cluster 2
    def check_slots():
        slots = cluster2.node(1).client.execute_command('CLUSTER', 'SLOTS')
        assert len(slots) == 2
        for i in range(2):
            if slots[i][2][1] < 5100:
                assert slots[i][0:2] == [0, 8191], slots
                assert slots[i][2][0] == b'localhost', slots
                assert slots[i][2][1] != 5001, slots

    assert_after(check_slots, 10)


def test_shard_group_no_slots(cluster):
    cluster.create(3, raft_args={
        'sharding': 'yes',
        'slot-config': ''
    })

    def validate_nodes(cluster_nodes):
        assert len(cluster_nodes) == 3

        for i in range(len(cluster_nodes)):
            node_data = cluster_nodes[i].split(b' ')
            assert len(node_data) == 9
            assert node_data[8] == b""

    validate_nodes(cluster.node(1).client.execute_command('CLUSTER', 'NODES').splitlines())


def test_shard_group_reshard_to_migrate(cluster):
    cluster.create(3, raft_args={
        'sharding': 'yes',
        'external-sharding': 'yes'
    })

    cluster.execute("set", "key", "value")

    assert cluster.execute(
        'RAFT.SHARDGROUP', 'REPLACE',
        '2',

        '12345678901234567890123456789013',
        '1', '1',
        '0', '16383', '2', '0',
        '3' * 40, '3.3.3.3:3333',

        cluster.leader_node().info()["raft_dbid"],
        '1', '1',
        '0', '16383', '3', '0',
        '2' * 40, '2.2.2.2:2222',
    ) == b'OK'

    assert cluster.execute("get", "key") == b'value'

    with raises(ResponseError, match="ASK 9189 3.3.3.3:3333"):
        cluster.execute("set", "key1", "value1")

    conn = cluster.leader_node().client.connection_pool.get_connection('deferred')
    conn.send_command('MULTI')
    assert conn.read_response() == b'OK'
    conn.send_command('set', 'key', 'newvalue')
    assert conn.read_response() == b'QUEUED'
    conn.send_command('set', '{key}key1', 'newvalue')
    assert conn.read_response() == b'QUEUED'
    conn.send_command('EXEC')
    with raises(ResponseError, match="TRYAGAIN"):
        conn.read_response()

    assert cluster.execute("del", "key") == 1

    with raises(ResponseError, match="ASK 12539 3.3.3.3:3333"):
        cluster.execute("get", "key")


def test_shard_group_reshard_to_import(cluster):
    cluster.create(3, raft_args={
        'sharding': 'yes',
        'external-sharding': 'yes'
    })

    cluster.execute("set", "key", "value")

    assert cluster.execute(
        'RAFT.SHARDGROUP', 'REPLACE',
        '2',

        '12345678901234567890123456789013',
        '1', '1',
        '0', '16383', '3', '456',
        '1234567890123456789012345678901334567890', '3.3.3.3:3333',

        cluster.leader_node().info()["raft_dbid"],
        '1', '1',
        '0', '16383', '2', '456',
        '1234567890123456789012345678901234567890', '2.2.2.2:2222',
    ) == b'OK'

    with raises(ResponseError, match="MOVED 12539 3.3.3.3:3333"):
        # can't use cluster.execute() as that will try to handle the MOVED response itself
        cluster.leader_node().client.get("key")

    conn = cluster.leader_node().client.connection_pool.get_connection('deferred')
    conn.send_command('ASKING')
    assert conn.read_response() == b'OK'

    conn.send_command('get', 'key')
    assert conn.read_response() == b'value'

    conn.send_command('ASKING')
    assert conn.read_response() == b'OK'

    conn.send_command('get', 'key1')
    with raises(ResponseError, match="TRYAGAIN"):
        conn.read_response()

    conn.send_command('ASKING')
    assert conn.read_response() == b'OK'

    conn.send_command("del", "key")
    assert conn.read_response() == 1

    conn.send_command('ASKING')
    assert conn.read_response() == b'OK'

    conn.send_command("get", "key")
    with raises(ResponseError, match="TRYAGAIN"):
        conn.read_response()


def test_asking_follower(cluster):
    cluster.create(3, raft_args={
        'sharding': 'yes',
        'external-sharding': 'yes'
    })

    cluster.execute("set", "key", "value")

    assert cluster.execute(
        'RAFT.SHARDGROUP', 'REPLACE',
        1,
        cluster.leader_node().info()["raft_dbid"],
        '1', '3',
        '0', '16383', '2', '123',
        '1234567890123456789012345678901234567890', cluster.node(1).address,
        '1234567890123456789012345678901234567891', cluster.node(2).address,
        '1234567890123456789012345678901234567892', cluster.node(3).address
    ) == b'OK'

    cluster.wait_for_unanimity()
    cluster.node(2).wait_for_log_applied()

    conn = cluster.node(2).client.connection_pool.get_connection('deferred')
    conn.send_command('ASKING')
    assert conn.read_response() == b'OK'

    conn.send_command('get', 'key')
    with raises(ResponseError, match="ASK"):
        conn.read_response()


def test_cluster_slots_for_empty_slot_sg(cluster):
    cluster.create(3, raft_args={'sharding': 'yes', 'external-sharding': 'yes'})
    cluster_shardgroup_id = "1" * 32

    assert cluster.execute(
        'RAFT.SHARDGROUP', 'REPLACE',
        '1',

        cluster_shardgroup_id,
        '0', '1',
        '%s00000001' % cluster_shardgroup_id, '1.1.1.1:1111',
        ) == b'OK'

    def validate_slots(cluster_slots):
        assert len(cluster_slots) == 0

    validate_slots(cluster.node(1).client.execute_command('CLUSTER', 'SLOTS'))


def test_cluster_slots_for_single_slot_sg(cluster):
    cluster.create(3, raft_args={'sharding': 'yes', 'external-sharding': 'yes'})
    cluster_stable_shardgroup_id = "1" * 32
    cluster_importing_shardgroup_id = "2" * 32
    cluster_migrating_shardgroup_id = "3" * 32

    assert cluster.execute(
        'RAFT.SHARDGROUP', 'REPLACE',
        '3',

        cluster_stable_shardgroup_id,
        '1', '1',
        '0', '0', '1', '123',
        '%s00000001' % cluster_stable_shardgroup_id, '1.1.1.1:1111',

        cluster_importing_shardgroup_id,
        '1', '1',
        '501', '501', '2', '123',
        '%s00000001' % cluster_importing_shardgroup_id, '2.2.2.2:2222',

        cluster_migrating_shardgroup_id,
        '1', '1',
        '501', '501', '3', '123',
        '%s00000001' % cluster_migrating_shardgroup_id, '3.3.3.3:3333',
        ) == b'OK'

    def validate_slots(cluster_slots):
        assert len(cluster_slots) == 2

        stable_node_id = "{}00000001".format(cluster_stable_shardgroup_id).encode()
        migrate_node_id = "{}00000001".format(cluster_migrating_shardgroup_id).encode()

        for i in range(len(cluster_slots)):
            node_id = cluster_slots[i][2][2]
            start_slot = cluster_slots[i][0]
            end_slot = cluster_slots[i][1]

            if node_id == stable_node_id:
                assert start_slot == 0, cluster_slots
                assert end_slot == 0, cluster_slots
            elif node_id == migrate_node_id:
                assert start_slot == 501, cluster_slots
                assert end_slot == 501, cluster_slots
            else:
                assert False, "failed to match id {}".format(node_id)

    validate_slots(cluster.node(1).client.execute_command('CLUSTER', 'SLOTS'))


def test_cluster_slots_for_single_slot_range_sg(cluster):
    cluster.create(3, raft_args={'sharding': 'yes', 'external-sharding': 'yes'})
    cluster_stable_shardgroup_id = "1" * 32
    cluster_importing_shardgroup_id = "2" * 32
    cluster_migrating_shardgroup_id = "3" * 32

    assert cluster.execute(
        'RAFT.SHARDGROUP', 'REPLACE',
        '3',

        cluster_stable_shardgroup_id,
        '1', '1',
        '0', '500', '1', '123',
        '%s00000001' % cluster_stable_shardgroup_id, '1.1.1.1:1111',

        cluster_importing_shardgroup_id,
        '1', '1',
        '501', '16383', '2', '123',
        '%s00000001' % cluster_importing_shardgroup_id, '2.2.2.2:2222',

        cluster_migrating_shardgroup_id,
        '1', '1',
        '501', '16383', '3', '123',
        '%s00000001' % cluster_migrating_shardgroup_id, '3.3.3.3:3333',
        ) == b'OK'

    def validate_slots(cluster_slots):
        assert len(cluster_slots) == 2

        stable_node_id = "{}00000001".format(cluster_stable_shardgroup_id).encode()
        migrate_node_id = "{}00000001".format(cluster_migrating_shardgroup_id).encode()

        for i in range(len(cluster_slots)):
            node_id = cluster_slots[i][2][2]
            start_slot = cluster_slots[i][0]
            end_slot = cluster_slots[i][1]

            if node_id == stable_node_id:
                assert start_slot == 0, cluster_slots
                assert end_slot == 500, cluster_slots
            elif node_id == migrate_node_id:
                assert start_slot == 501, cluster_slots
                assert end_slot == 16383, cluster_slots
            else:
                assert False, "failed to match id {}".format(node_id)

    validate_slots(cluster.node(1).client.execute_command('CLUSTER', 'SLOTS'))


def test_cluster_slots_for_multiple_slots_range_sg(cluster):
    cluster.create(3, raft_args={'sharding': 'yes', 'external-sharding': 'yes'})
    shardgroup_id_1 = "1" * 32
    shardgroup_id_2 = "2" * 32
    shardgroup_id_3 = "3" * 32

    assert cluster.execute(
        'RAFT.SHARDGROUP', 'REPLACE',
        '3',

        shardgroup_id_1,
        '3', '1',
        '0', '500', '1', '123',
        '600', '700', '2', '123',
        '800', '1000', '3', '123',
        '%s00000001' % shardgroup_id_1, '1.1.1.1:1111',

        shardgroup_id_2,
        '2', '1',
        '1001', '16383', '2', '123',
        '600', '700', '3', '123',
        '%s00000001' % shardgroup_id_2, '2.2.2.2:2222',

        shardgroup_id_3,
        '2', '1',
        '800', '1000', '2', '123',
        '1001', '16383', '3', '123',
        '%s00000001' % shardgroup_id_3, '3.3.3.3:3333',
        ) == b'OK'

    def validate_slots(cluster_slots):
        assert len(cluster_slots) == 4

        node_id_1 = "{}00000001".format(shardgroup_id_1).encode()
        node_id_2 = "{}00000001".format(shardgroup_id_2).encode()
        node_id_3 = "{}00000001".format(shardgroup_id_3).encode()

        for i in range(len(cluster_slots)):
            node_id = cluster_slots[i][2][2]
            start_slot = cluster_slots[i][0]
            end_slot = cluster_slots[i][1]

            if start_slot == 0 and end_slot == 500:
                assert node_id == node_id_1
            elif start_slot == 800 and end_slot == 1000:
                assert node_id == node_id_1
            elif start_slot == 600 and end_slot == 700:
                assert node_id == node_id_2
            elif start_slot == 1001 and end_slot == 16383:
                assert node_id == node_id_3
            else:
                assert False, "failed to match slot {}-{}".format(start_slot, end_slot)

    validate_slots(cluster.node(1).client.execute_command('CLUSTER', 'SLOTS'))


def test_cluster_nodes_for_empty_slot_sg(cluster):
    cluster.create(3, raft_args={'sharding': 'yes', 'external-sharding': 'yes'})
    cluster_shardgroup_id = "1" * 32

    assert cluster.execute(
        'RAFT.SHARDGROUP', 'REPLACE',
        '1',

        cluster_shardgroup_id,
        '0', '1',
        '%s00000001' % cluster_shardgroup_id, '1.1.1.1:1111',
        ) == b'OK'

    def validate_nodes(cluster_nodes):
        assert len(cluster_nodes) == 1
        cluster_dbid = cluster.leader_node().info()["raft_dbid"]
        node_id = "{}00000001".format(cluster_shardgroup_id).encode()
        local_node_id = "{}00000001".format(cluster_dbid).encode()

        node_data = cluster_nodes[0].split(b' ')

        if local_node_id == node_data[0]:
            assert node_data[2] == b"myself,master"
        else:
            assert node_data[2] == b"master"

        assert node_data[3] == b"-"

        assert node_data[0] == node_id
        assert node_data[8] == b""

    validate_nodes(cluster.node(1).execute('CLUSTER', 'NODES').splitlines())


def test_cluster_nodes_for_single_slot_range_sg(cluster):
    cluster.create(3, raft_args={'sharding': 'yes', 'external-sharding': 'yes'})
    cluster_stable_shardgroup_id = "1" * 32
    cluster_importing_shardgroup_id = "2" * 32
    cluster_migrating_shardgroup_id = "3" * 32

    assert cluster.execute(
        'RAFT.SHARDGROUP', 'REPLACE',
        '3',

        cluster_stable_shardgroup_id,
        '1', '1',
        '0', '500', '1', '123',
        '%s00000001' % cluster_stable_shardgroup_id, '1.1.1.1:1111',

        cluster_importing_shardgroup_id,
        '1', '1',
        '501', '16383', '2', '123',
        '%s00000001' % cluster_importing_shardgroup_id, '2.2.2.2:2222',

        cluster_migrating_shardgroup_id,
        '1', '1',
        '501', '16383', '3', '123',
        '%s00000001' % cluster_migrating_shardgroup_id, '3.3.3.3:3333',
        ) == b'OK'

    def validate_nodes(cluster_nodes):
        assert len(cluster_nodes) == 3
        cluster_dbid = cluster.leader_node().info()["raft_dbid"]
        stable_node_id = "{}00000001".format(cluster_stable_shardgroup_id).encode()
        migrate_node_id = "{}00000001".format(cluster_migrating_shardgroup_id).encode()
        import_node_id = "{}00000001".format(cluster_importing_shardgroup_id).encode()
        local_node_id = "{}00000001".format(cluster_dbid).encode()

        for i in range(len(cluster_nodes)):
            node_data = cluster_nodes[i].split(b' ')

            if local_node_id == node_data[0]:
                assert node_data[2] == b"myself,master"
            else:
                assert node_data[2] == b"master"

            assert node_data[3] == b"-"

            if node_data[0] == stable_node_id:
                assert node_data[8] == b"0-500"
            elif node_data[0] == migrate_node_id:
                assert node_data[8] == b"501-16383"
            elif node_data[0] == import_node_id:
                assert node_data[8] == b""
            else:
                assert False, "failed to match id {}".format(node_data[0])

    validate_nodes(cluster.node(1).execute('CLUSTER', 'NODES').splitlines())


def test_cluster_nodes_for_single_slot_sg(cluster):
    cluster.create(3, raft_args={'sharding': 'yes', 'external-sharding': 'yes'})
    cluster_stable_shardgroup_id = "1" * 32
    cluster_importing_shardgroup_id = "2" * 32
    cluster_migrating_shardgroup_id = "3" * 32

    assert cluster.execute(
        'RAFT.SHARDGROUP', 'REPLACE',
        '3',

        cluster_stable_shardgroup_id,
        '1', '1',
        '0', '0', '1', '123',
        '%s00000001' % cluster_stable_shardgroup_id, '1.1.1.1:1111',

        cluster_importing_shardgroup_id,
        '1', '1',
        '501', '501', '2', '123',
        '%s00000001' % cluster_importing_shardgroup_id, '2.2.2.2:2222',

        cluster_migrating_shardgroup_id,
        '1', '1',
        '501', '501', '3', '123',
        '%s00000001' % cluster_migrating_shardgroup_id, '3.3.3.3:3333',
        ) == b'OK'

    def validate_nodes(cluster_nodes):
        assert len(cluster_nodes) == 3
        cluster_dbid = cluster.leader_node().info()["raft_dbid"]
        stable_node_id = "{}00000001".format(cluster_stable_shardgroup_id).encode()
        migrate_node_id = "{}00000001".format(cluster_migrating_shardgroup_id).encode()
        import_node_id = "{}00000001".format(cluster_importing_shardgroup_id).encode()
        local_node_id = "{}00000001".format(cluster_dbid).encode()

        for i in range(len(cluster_nodes)):
            node_data = cluster_nodes[i].split(b' ')

            if local_node_id == node_data[0]:
                assert node_data[2] == b"myself,master"
            else:
                assert node_data[2] == b"master"

            assert node_data[3] == b"-"

            if node_data[0] == stable_node_id:
                assert node_data[8] == b"0"
            elif node_data[0] == migrate_node_id:
                assert node_data[8] == b"501"
            elif node_data[0] == import_node_id:
                assert node_data[8] == b""
            else:
                assert False, "failed to match id {}".format(node_data[0])

    validate_nodes(cluster.node(1).execute('CLUSTER', 'NODES').splitlines())


def test_cluster_nodes_for_multiple_slots_range_sg(cluster):
    cluster.create(3, raft_args={'sharding': 'yes', 'external-sharding': 'yes'})
    shardgroup_id_1 = "1" * 32
    shardgroup_id_2 = "2" * 32
    shardgroup_id_3 = "3" * 32

    assert cluster.execute(
        'RAFT.SHARDGROUP', 'REPLACE',
        '3',

        shardgroup_id_1,
        '3', '1',
        '0', '500', '1', '123',
        '600', '700', '2', '123',
        '800', '1000', '3', '123',
        '%s00000001' % shardgroup_id_1, '1.1.1.1:1111',

        shardgroup_id_2,
        '2', '1',
        '1001', '16383', '2', '123',
        '600', '700', '3', '123',
        '%s00000001' % shardgroup_id_2, '2.2.2.2:2222',

        shardgroup_id_3,
        '2', '1',
        '800', '1000', '2', '123',
        '1001', '16383', '3', '123',
        '%s00000001' % shardgroup_id_3, '3.3.3.3:3333',
        ) == b'OK'

    def validate_nodes(cluster_nodes):
        assert len(cluster_nodes) == 3
        cluster_dbid = cluster.leader_node().info()["raft_dbid"]
        node_id_1 = "{}00000001".format(shardgroup_id_1).encode()
        node_id_2 = "{}00000001".format(shardgroup_id_2).encode()
        node_id_3 = "{}00000001".format(shardgroup_id_3).encode()
        local_node_id = "{}00000001".format(cluster_dbid).encode()

        for i in range(len(cluster_nodes)):
            node_data = cluster_nodes[i].split(b' ')

            if local_node_id == node_data[0]:
                assert node_data[2] == b"myself,master"
            else:
                assert node_data[2] == b"master"

            assert node_data[3] == b"-"

            if node_data[0] == node_id_1:
                assert node_data[8] == b"0-500"
                assert node_data[9] == b"800-1000"
            elif node_data[0] == node_id_2:
                assert node_data[8] == b"600-700"
            elif node_data[0] == node_id_3:
                assert node_data[8] == b"1001-16383"
            else:
                assert False, "failed to match id {}".format(node_data[0])

    validate_nodes(cluster.node(1).execute('CLUSTER', 'NODES').splitlines())
