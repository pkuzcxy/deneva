#!/usr/bin/python

from __future__ import print_function
from fabric.api import task,run,local,put,get,execute,settings
from fabric.decorators import *
from fabric.context_managers import shell_env,quiet
from fabric.exceptions import *
from fabric.utils import puts,fastprint
from time import sleep
from contextlib import contextmanager
import traceback
import os,sys,datetime,re
import itertools
import glob
import shlex
import subprocess
import pprint

sys.path.append('..')

from environment import *
from experiments import *
from helper import get_cfgs,get_outfile_name

COLORS = {
    "info"  : 32, #green
    "warn"  : 33, #yellow
    "error" : 31, #red
    "debug" : 36, #cyan
}

#OUT_FMT = "[{h}] {p}: {fn}:".format
PP = pprint.PrettyPrinter(indent=4)

NOW=datetime.datetime.now()
STRNOW=NOW.strftime("%Y%m%d-%H%M%S")

os.chdir('../..')

MAX_TIME_PER_EXP = 60 * 8   # in seconds

#cfgs = configs

EXECUTE_EXPS = True
SKIP = False
CC_ALG = ""

set_env()

@task
@hosts('localhost')
def using_vcloud():
    set_env_vcloud()

@task
@hosts('localhost')
def using_istc():
    set_env_istc()

@task
@hosts('localhost')
def using_local():
    set_env_local()

## Basic usage:
##      fab using_vcloud run_exps:experiment_1
##      fab using_local  run_exps:experiment_1
##      fab using_istc   run_exps:experiment_1
@task
@hosts('localhost')
def run_exps(exps,skip_completed='False',exec_exps='True',dry_run='False'):
    global SKIP, EXECUTE_EXPS 
    SKIP = skip_completed == 'True'
    EXECUTE_EXPS = exec_exps == 'True'
    env.dry_run = dry_run == 'True'
    if env.dry_run:
        with color(level="warn"):
            puts("this will be a dry run!",show_prefix=True)
        with color():
            puts("running experiment set:{}".format(exps),show_prefix=True)
    execute(run_exp,exps)


## Basic usage:
##      fab using_vcloud network_test
##      fab using_istc   network_test:4
@task
@hosts(['localhost'])
def network_test(num_nodes=16,exps="network_experiment",skip_completed='False',exec_exps='True'):
    env.batch_mode = False
    global SKIP, EXECUTE_EXPS, MAX_TIME_PER_EXP
    SKIP = skip_completed == 'True'
    EXECUTE_EXPS = exec_exps == 'True'
    MAX_TIME_PER_EXP = 30
    num_nodes = int(num_nodes)
    if num_nodes < 2 or len(env.hosts) < num_nodes:
        with color(level="error"):
            puts("not enough hosts in ifconfig!",show_prefix=True)
            abort()
    exp_hosts=env.hosts[0:num_nodes]
    pairs = list(itertools.combinations(exp_hosts,2))
    for pair in pairs:
        set_hosts(list(pair))
        execute(run_exp,exps,network_test=True)


@task
@hosts('localhost')
def delete_local_results():
    local("rm -f results/*");

@task
@parallel
def copy_files(schema):
    executable_files = ["rundb","runcl"]
    if CC_ALG == "CALVIN":
        executable_files.append("runsq")
    files = ["ifconfig.txt"]
    files.append(schema)
    succeeded = True
    with settings(warn_only=True):
        for f in (files + executable_files):
            res = put(f,env.rem_homedir,mirror_local_mode=True)
            if not res.succeeded:
                with color("warn"):
                    puts("WARN: put: {} -> {} failed!".format(f,env.rem_homedir),show_prefix=True)
                succeeded = False
                break
        if not succeeded:
            with color("warn"):
                puts("WARN: killing all executables and retrying...",show_prefix=True)
            killall()
            # If this fails again then we abort
            for f in (files + executable_files):
                res = put(f,env.rem_homedir,mirror_local_mode=True)
            if not res.succeeded:
                with color("error"):
                    puts("ERROR: put: {} -> {} failed! (2nd attempt)... Aborting".format(f,env.rem_homedir),show_prefix=True)
                    abort()

@task
@parallel
def sync_clocks(max_offset=0.01,max_attempts=5,delay=15):
    if env.dry_run:
        return True
    offset = sys.float_info.max
    attempts = 0
    while attempts < max_attempts:
        res = run("ntpdate -q clock-1.cs.cmu.edu")
        offset = float(res.stdout.split(",")[-2].split()[-1])
        #print "Host ",env.host,": offset = ",offset
        if abs(offset) < max_offset:
            break
        sleep(delay)
        res = run("ntpdate -b clock-1.cs.cmu.edu")
        sleep(delay)
        attempts += 1
    return attempts < max_attempts
        

@task
@hosts('localhost')
def compile():
    compiled = False
    with quiet():
        compiled = local("make clean; make -j",capture=True).succeeded
    if not compiled:
        with settings(warn_only=True):
            compiled = local("make -j") # Print compilation errors
            if not compiled:
                with color("error"):
                    puts("ERROR: cannot compile code!",show_prefix=True)
                


@task
@parallel
def killall():
    with settings(warn_only=True):
        if not env.dry_run:
            run("pkill -f rundb")
            run("pkill -f runcl")
            run("pkill -f runsq")

@task
@parallel
def run_cmd(cmd):
    run(cmd)

@task
@parallel
def deploy(schema_path,nids):
    nid = nids[env.host]
    succeeded = True
    with shell_env(SCHEMA_PATH=schema_path):
        # TODO: remove this assertion after debugging
        assert schema_path == '/root/'
        with settings(warn_only=True,command_timeout=MAX_TIME_PER_EXP):
            if env.host in env.roledefs["servers"]:
                cmd = "./rundb -nid{} >> results.out 2>&1".format(nid)  
            elif env.host in env.roledefs["clients"]:
                cmd = "./runcl -nid{} >> results.out 2>&1".format(nid)
            elif "sequencer" in env.roledefs and env.host in env.roledefs["sequencer"]:
                cmd = "./runsq -nid{} >> results.out 2>&1".format(nid)
            else:
                with color('error'):
                    puts("host does not belong to any roles",show_prefix=True)
                    puts("current roles:",show_prefix=True)
                    puts(pprint.pformat(env.roledefs,depth=3),show_prefix=False)

            try:
                res = run("echo $SCHEMA_PATH")
                if not env.dry_run:
                    run(cmd)
            except CommandTimeout:
                pass
            except NetworkError:
                pass
    return True

@task
@parallel
def get_results(outfiles):
    succeeded = True
    nid = env.hosts.index(env.host)
    rem_path=os.path.join(env.rem_homedir,"results.out")
    loc_path=os.path.join(env.result_dir, outfiles[env.host])
    with settings(warn_only=True):
        if not env.dry_run:
            res1 = get(remote_path=rem_path, local_path=loc_path)
            res2 = run("rm -f results.out")
            succeeded = res1.succeeded and res2.succeeded
    return succeeded

@task
@hosts('localhost')
def write_config(cfgs):
    dbx_cfg = os.path.join(env.local_path,"config.h")
    f = open(dbx_cfg,'r');
    lines = f.readlines()
    f.close()
    with open(dbx_cfg,'w') as f_cfg:
        for line in lines:
            found_cfg = False
            for c in cfgs:
                found_cfg = re.search("#define "+c + "\t",line) or re.search("#define "+c + " ",line);
                if found_cfg:
                    f_cfg.write("#define " + c + " " + str(cfgs[c]) + "\n")
                    break
            if not found_cfg: f_cfg.write(line)

@task
@hosts('localhost')
def write_ifconfig(roles):
    with color():
        puts("writing roles to the ifconfig file:",show_prefix=True)
        puts(pprint.pformat(roles,depth=3),show_prefix=False)
    nids = {}
    nid = 0
    with open("ifconfig.txt",'w') as f:
        for server in roles['servers']:
            f.write(server + "\n")
            nids[server] = nid
            nid += 1
        for client in roles['clients']:
            f.write(client + "\n")
            nids[client] = nid
            nid += 1
        if "sequencer" in roles:
            assert CC_ALG == "CALVIN"
            sequencer = roles['sequencer'][0]
            f.write(sequencer + "\n")
            nids[sequencer] = nid
            nid += 1
    return nids
            
@task
@hosts('localhost')
def assign_roles(server_cnt,client_cnt,append=False):
    if len(env.hosts) < server_cnt+client_cnt:
        with color("error"):
            puts("ERROR: not enough hosts to run experiment",show_prefix=True)
            puts("\tHosts required: {}".format(server_cnt+client_cnt))
            puts("\tHosts available: {} ({})".format(len(env.hosts),pprint.pformat(env.hosts,depth=3)))
    assert len(env.hosts) >= server_cnt+client_cnt
    new_roles = {}
    servers=env.hosts[0:server_cnt]
    clients=env.hosts[server_cnt:server_cnt+client_cnt]
    if CC_ALG == 'CALVIN':
        sequencer = env.hosts[server_cnt+client_cnt:server_cnt+client_cnt+1]
    if env.roledefs is None or len(env.roledefs) == 0: 
        env.roledefs={}
        env.roledefs['clients']=[]
        env.roledefs['servers']=[]
        if CC_ALG == 'CALVIN':
            env.roledefs['sequencer']=[]
    if append:
        env.roledefs['clients'].extend(clients)
        env.roledefs['servers'].extend(servers)
        if CC_ALG == 'CALVIN':
            env.roledefs['sequencer'].extend(sequencer)
    else:
        env.roledefs['clients']=clients
        env.roledefs['servers']=servers
        if CC_ALG == 'CALVIN':
            env.roledefs['sequencer']=sequencer
    new_roles['clients']=clients
    new_roles['servers']=servers
    if CC_ALG == 'CALVIN':
        new_roles['sequencer']=sequencer
    with color():
        puts("Assigned the following roles:",show_prefix=True)
        puts(pprint.pformat(new_roles,depth=3) + "\n",show_prefix=False)
        puts("Updated env roles:",show_prefix=True)
        puts(pprint.pformat(env.roledefs,depth=3) + "\n",show_prefix=False)
    return new_roles

def get_good_hosts():
    good_hosts = []
    set_hosts()

    # Find and skip bad hosts
    ping_results = execute(ping)
    for host in ping_results:
        if ping_results[host] == 0:
            good_hosts.append(host)
        else:
            with color("warn"):
                puts("Skipping non-responsive host {}".format(host),show_prefix=True)
    return good_hosts

@task
@hosts(['localhost'])
def run_exp(expss,network_test=False):
    # TODO: fix this
    exps = []
    exps.append(expss)
    schema_path = "{}/".format(env.rem_homedir)
    good_hosts = []
    if not network_test and EXECUTE_EXPS:
        good_hosts = get_good_hosts()
        with color():
            puts("good host list =\n{}".format(pprint.pformat(good_hosts,depth=3)),show_prefix=True)
    for exp in exps:
        fmt,experiments = experiment_map[exp]()
        batch_size = 0 
        nids = {} 
        outfiles = {}
        for e in experiments:
            cfgs = get_cfgs(fmt,e)
            if env.remote:
                cfgs["TPORT_TYPE"],cfgs["TPORT_TYPE_IPC"],cfgs["TPORT_PORT"]="\"tcp\"","false",7000
            
            output_f = get_outfile_name(cfgs,env.hosts) 

            # Check whether experiment has been already been run in this batch
            if SKIP:
                if len(glob.glob('{}*{}*.out'.format(env.result_dir,output_f))) > 0:
                    with color("warn"):
                        puts("experiment exists in results folder... skipping",show_prefix=True)
                    continue

            output_dir = output_f + "/"
            output_f = output_f + STRNOW 

            write_config(cfgs)
            global CC_ALG
            CC_ALG = cfgs["CC_ALG"]
            execute(compile)
            if EXECUTE_EXPS:
                cmd = "mkdir -p {}".format(env.result_dir)
                local(cmd)
                cmd = "cp config.h {}{}.cfg".format(env.result_dir,output_f)
                local(cmd)

                nnodes = cfgs["NODE_CNT"]
                nclnodes = cfgs["CLIENT_NODE_CNT"]
                ntotal = nnodes + nclnodes
                if CC_ALG == 'CALVIN':
                    ntotal += 1

                if env.remote:
                    if not network_test:
                        set_hosts(good_hosts)
                    if ntotal > len(env.hosts):
                        msg = "Not enough nodes to run experiment!\n"
                        msg += "\tRequired nodes: {}, ".format(ntotal)
                        msg += "Actual nodes: {}".format(len(env.hosts))
                        with color():
                            puts(msg,show_prefix=True)
                        cmd = "rm -f config.h {}{}.cfg".format(env.result_dir,output_f)
                        local(cmd)
                        continue
                        
                    if env.batch_mode:
                        # If full, execute all exps in batch and reset everything
                        full = (batch_size + ntotal) > len(env.hosts)
                        if full:
                            if env.cluster != 'istc':
                                # Sync clocks before each experiment
                                execute(sync_clocks)
                            with color():
                                puts("Batch is full, deploying batch...",show_prefix=True)
                            with color("debug"):
                                puts(pprint.pformat(outfiles,depth=3),show_prefix=False)
                            set_hosts(env.hosts[:batch_size])
                            execute(deploy,schema_path,nids)
                            execute(get_results,outfiles)
                            good_hosts = get_good_hosts()
                            env.roledefs = None
                            batch_size = 0
                            nids = {}
                            outfiles = {}
                            set_hosts(good_hosts)
                        else:
                            with color():
                                puts("Adding experiment to current batch: {}".format(output_f), show_prefix=True)
                        machines = env.hosts[batch_size : batch_size + ntotal]
                        batch_size += ntotal
                    else:
                        machines = env.hosts[:ntotal]

                    set_hosts(machines)
                    new_roles=execute(assign_roles,nnodes,nclnodes,append=env.batch_mode)[env.host]
                    new_nids = execute(write_ifconfig,new_roles)[env.host]
                    nids.update(new_nids)
                    for host,nid in new_nids.iteritems():
                        outfiles[host] = "{}_{}.out".format(nid,output_f) 

                    if cfgs["WORKLOAD"] == "TPCC":
                        schema = "benchmarks/TPCC_short_schema.txt"
                    elif cfgs["WORKLOAD"] == "YCSB":
                        schema = "benchmarks/YCSB_schema.txt"
                    # NOTE: copy_files will fail if any (possibly) stray processes
                    # are still running one of the executables. Setting the 'kill'
                    # flag in environment.py to true to kill these processes. This
                    # is useful for running real experiments but dangerous when both
                    # of us are debugging...
                    execute(copy_files,schema)
                    
                    last_exp = experiments.index(e) == len(experiments) - 1
                    if not env.batch_mode or last_exp:
                        if env.batch_mode:
                            #set_hosts(good_hosts)
                            set_hosts(env.hosts[:batch_size])
                            print("Deploying last batch")
                        else:
                            print("Deploying: {}".format(output_f))
                        if env.cluster != 'istc':
                            # Sync clocks before each experiment
                            print("Syncing Clocks...")
                            execute(sync_clocks)
                        execute(deploy,schema_path,nids)
                        execute(get_results,outfiles)
                        good_hosts = get_good_hosts()
                        set_hosts(good_hosts)
                        batch_size = 0
                        nids = {}
                        outfiles = {}
                        env.roledefs = None
                else:
                    pids = []
                    print("Deploying: {}".format(output_f))
                    for n in range(ntotal):
                        if n < nnodes:
                            cmd = "./rundb -nid{}".format(n)
                        elif n < nnodes+nclnodes:
                            cmd = "./runcl -nid{}".format(n)
                        elif n == nnodes+nclnodes:
                            assert(CC_ALG == 'CALVIN')
                            cmd = "./runsq -nid{}".format(n)
                        else:
                            assert(false)
                        print(cmd)
                        cmd = shlex.split(cmd)
                        ofile_n = "{}{}_{}.out".format(env.result_dir,n,output_f)
                        ofile = open(ofile_n,'w')
                        p = subprocess.Popen(cmd,stdout=ofile,stderr=ofile)
                        pids.insert(0,p)
                    for n in range(ntotal):
                        pids[n].wait()


def succeeded(outcomes):
    for host,outcome in outcomes.iteritems():
        if not outcome:
            return False
    return True

@task
@parallel
def ping():
    with settings(warn_only=True):
        res=local("ping -w8 -c1 {}".format(env.host),capture=True)
    assert res != None
    return res.return_code

@contextmanager
def color(level="info"):
    if not level in COLORS:
        level = "info"
    print("\033[%sm" % COLORS[level],end="")
    yield
    print("\033[0m",end="")

