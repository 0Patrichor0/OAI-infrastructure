#!/usr/bin/env python3
"""Build preserved source directly with CMake; never start services."""
import argparse,os,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def run(*args,cwd=None):subprocess.run([str(x) for x in args],cwd=cwd,check=True)
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--home',type=Path,required=True);ap.add_argument('--jobs',type=int,default=3)
    ap.add_argument('--skip-nwdaf',action='store_true');a=ap.parse_args();home=a.home.resolve();oai=home/'oai-ran-e2';flex=oai/'openair2/E2AP/flexric'
    if not Path('/opt/asn1c/bin/asn1c').exists():
        raise SystemExit('Build/install the pinned ASN1 compiler first; see docs/REUSE.md')
    fbuild=flex/'build';build=oai/'cmake_targets/ran_build/build'
    flags=['-G','Ninja','-DCMAKE_C_COMPILER=/usr/bin/gcc-13','-DCMAKE_CXX_COMPILER=/usr/bin/g++-13','-DCMAKE_BUILD_TYPE=RelWithDebInfo','-DCMAKE_POLICY_VERSION_MINIMUM=3.5','-DE2AP_VERSION=E2AP_V3','-DKPM_VERSION=KPM_V3_00']
    run('cmake','-S',flex,'-B',fbuild,*flags,'-DUNIT_TEST=FALSE','-DCMAKE_INSTALL_PREFIX=/usr/local')
    run('cmake','--build',fbuild,'--parallel',a.jobs)
    run('cmake','-S',oai,'-B',build,*flags,'-DE2_AGENT=ON','-DAVX512=OFF','-DAVX2=ON','-DOAI_SIMU=ON','-DOAI_USRP=ON','-DASN1C_EXEC=/opt/asn1c/bin/asn1c','-DASN1C_EXEC_PATH=/opt/asn1c/bin/asn1c')
    run('cmake','--build',build,'--parallel',a.jobs,'--target','nr-softmodem','nr-cuup','nr-uesoftmodem','rfsimulator','oai_usrpdevif','params_libconfig','params_yaml','coding','dfts','nr_dlbench')
    control=home/'intent-driven/ran'
    for name in ('mcs','prb'):
        run('gcc-13','-g','-DASN','-DE2AP_V3','-DKPM_V3_00','-DSQLITE3_XAPP','-I'+str(flex/'src'),'-I'+str(flex/'src/xApp'),'-I'+str(control),'-iquote',flex/'examples/xApp/c/helloworld',control/f'xapp_{name}_control.c',flex/'src/util/alg_ds/alg/defer.c',fbuild/'src/xApp/libe42_xapp.a','-pthread','-lsctp','-ldl',fbuild/'examples/xApp/c/monitor/RRC_MESSAGES/libasn1_nr_rrc.a','-o',control/f'xapp_{name}_control')
    if not a.skip_nwdaf:
        anlf=home/'nwdaf-rel17/anlf'
        for component,short in [('nbi-analytics','nbi-analytics'),('nbi-events','nbi-events'),('nbi-ml','nbi-ml'),('engine','engine'),('engine-ads','engine-ads'),('sbi','sbi')]:
            folder=anlf/'components'/('oai-nwdaf-'+component)
            run('docker','build','--network=host','--target','oai-nwdaf-'+component,'--tag','oai-nwdaf-'+component+':latest','--file',folder/'docker'/('Dockerfile.'+short),folder)
        run('docker','compose','build',cwd=home/'nwdaf-rel17/mtlf')
if __name__=='__main__':main()
