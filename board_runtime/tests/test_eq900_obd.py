import pytest
from codrive.eq900_obd import ReadGuard,Transaction,QUERIES,FC,packet,decode


def test_fixed_queries_only_and_rate_limit():
    g=ReadGuard();g.check(QUERIES[0][0],packet(QUERIES[0][2]),1.)
    with pytest.raises(ValueError,match='rate'):g.check(QUERIES[1][0],packet(QUERIES[1][2]),1.05)
    g.check(QUERIES[1][0],packet(QUERIES[1][2]),1.1)
    for ident,data in [(0x7df,packet(bytes.fromhex('010D'))),(0x7D4,packet(bytes.fromhex('1101'))),(0x7D4,FC)]:
        with pytest.raises(ValueError):g.check(ident,data,2.)
    with pytest.raises(ValueError):g.check(QUERIES[0][0],packet(QUERIES[0][2]),2.,extended=True)
    g.flow_target=0x7D4;g.check(0x7D4,FC,2.)
    with pytest.raises(ValueError):g.check(0x7D4,FC,2.)


def test_isotp_reassembly_and_steering_sign_units():
    t=Transaction();assert t.request(1.)[0]==0x7D4
    p=bytes.fromhex('6101')+bytes(9)+(-900).to_bytes(2,'big',signed=True)
    result,flow=t.receive(0x7DC,bytes([0x10,len(p)])+p[:6],1.01)
    assert result is None and flow==(0x7D4,FC)
    result,flow=t.receive(0x7DC,b'\x21'+p[6:],1.02)
    assert decode(result[0],result[1])=={'steering_wheel_deg':-90.}
    assert t.item is None


def test_brake_and_tcm_offsets_strip_response_header():
    assert decode(0x7A8,bytes.fromhex('62B008')+bytes(6)+b'\x10')=={'brake_pressed':True}
    assert decode(0x7A8,bytes.fromhex('62B008')+bytes(7))=={'brake_pressed':False}
    data=bytearray(9);data[6]=36;data[8]=128
    assert decode(0x7E9,bytes.fromhex('6201A0')+data)=={'speed_mps':10.,'accelerator_pct':pytest.approx(128*100/255)}
    assert decode(0x7E9,bytes.fromhex('6201A0'))=={}


def test_timeout_and_bad_sequence_release_transaction_without_retry():
    t=Transaction();t.request(1.)
    p=bytes.fromhex('6101')+bytes(11)
    t.receive(0x7DC,bytes([0x10,len(p)])+p[:6],1.01)
    assert t.receive(0x7DC,b'\x22'+p[6:],1.02)==(None,None)
    assert t.sequence_errors==1 and t.item is None
    assert t.request(1.1)[0]==0x7A0
    assert t.request(1.51)[0]==0x7E1 and t.timeouts==1


def test_negative_and_unrelated_responses_cannot_become_vehicle_values():
    t=Transaction();t.request(1.)
    assert t.receive(0x7E9,b'\x03\x62\x01\xa0',1.01)==(None,None)
    assert t.item is not None
    assert t.receive(0x7DC,b'\x03\x7f\x21\x11',1.02)==(None,None)
    assert t.item is None and t.negative_responses==1
