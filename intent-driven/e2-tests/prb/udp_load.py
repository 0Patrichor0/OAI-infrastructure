"""Bounded downlink traffic generator using only Python's standard library."""
import argparse,json,socket,time
p=argparse.ArgumentParser();p.add_argument('mode',choices=['server','client']);p.add_argument('--port',type=int,default=45201);p.add_argument('--ip',default='192.168.73.135');p.add_argument('--seconds',type=float,default=8);p.add_argument('--clients',type=int,default=2);a=p.parse_args()
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.settimeout(1)
if a.mode=='server':
    s.bind((a.ip,a.port));peers=set();deadline=time.monotonic()+20
    while len(peers)<a.clients and time.monotonic()<deadline:
        try:_,peer=s.recvfrom(2048);peers.add(peer)
        except socket.timeout:pass
    assert len(peers)==a.clients,'load clients did not register'
    payload=b'x'*1200;count=0;end=time.monotonic()+a.seconds;start=time.monotonic()
    while time.monotonic()<end:
        for _ in range(20):
            for peer in peers:s.sendto(payload,peer);count+=1
        time.sleep(.002)
    print(json.dumps({'peers':list(peers),'sent_bytes':count*len(payload),'seconds':time.monotonic()-start}),flush=True)
else:
    s.bind((a.ip,0));s.sendto(b'register',('192.168.73.135',a.port));count=0;end=time.monotonic()+a.seconds+3
    while time.monotonic()<end:
        try:count+=len(s.recv(2048))
        except socket.timeout:pass
    print(json.dumps({'ip':a.ip,'received_bytes':count}),flush=True)
