#!/usr/bin/env python3

import argparse
import copy
import re
import os
import random
import tempfile
import subprocess
import time
import sys
import json
from datetime import datetime

from jinja2 import Template
from kubescaler.scaler.google import GKECluster
import kubescaler.utils as utils
from kubernetes import client, config, watch

here = os.path.abspath(os.path.dirname(__file__))

# Keep a cache of current nodes we know about, and creation / removal times
cli = None
watcher = watch.Watch()

minicluster_template = """
apiVersion: flux-framework.org/v1alpha2
kind: MiniCluster
metadata:
  name: {{ name }}
spec:
  size: {{ size }}
  tasks: {{ tasks }}
  logging:
    quiet: true
  network: 
    headlessName: {{ service_name }}

  # The workers should not fail when they clean up
  flux:
    completeWorkers: true

  # This is a list because a pod can support multiple containers
  containers:
    - image: ghcr.io/converged-computing/metric-lammps:latest

      # You can set the working directory if your container WORKDIR is not correct.
      workingDir: /opt/lammps/examples/reaxff/HNS
      command: lmp -v x {{ x }} -v y {{ y }} -v z {{ z }} -in in.reaxc.hns -nocite
      resources:
        limits:
          cpu: {{ cpu_limit }}
        requests:
          cpu: {{ cpu_limit }}
"""


ensemble_template = """
apiVersion: ensemble.flux-framework.org/v1alpha1
kind: Ensemble
metadata:
  name: {{ name }}
spec:  
  members:

    # This is how you change the sidcar image, if needed. This is the one
    # that I push and use for development. Pull always ensures we get latest
  - sidecar:
      pullAlways: true
      image: ghcr.io/converged-computing/ensemble-operator-api:rockylinux9

    # Algorithm and options:
    # This is the algorithm run by the operator. The options are passed to
    # the running queue to further alter the outcome.
    # terminateChecks says to terminate after 2 subsequent inactive status checks
    algorithm:
      name: workload-demand
      options:
        terminateChecks: 2
        scaleUpChecks: 1
        order: "{{ order }}"

    # These are slightly different - flux gets to use the full node capacity
    # so 4 cores per node, tasks == size * 4
    jobs:
      - name: lammps-2
        command: lmp -v x {{x}} -v y {{y}} -v z {{z}} -in in.reaxc.hns -nocite
        count: 10
        nodes: 2
        tasks: 8
      - name: lammps-4
        command: lmp -v x {{x}} -v y {{y}} -v z {{z}} -in in.reaxc.hns -nocite
        count: 10
        nodes: 4
        tasks: 16
      - name: lammps-6
        command: lmp -v x {{x}} -v y {{y}} -v z {{z}} -in in.reaxc.hns -nocite
        count: 10
        nodes: 6
        tasks: 24
      - name: lammps-8
        command: lmp -v x {{x}} -v y {{y}} -v z {{z}} -in in.reaxc.hns -nocite
        count: 10
        nodes: 8
        tasks: 32

    minicluster:
      spec:
        size: {{ size }}
        minSize: {{ min_size }}
        maxSize: {{ max_size }}
        
        # The workers should not fail when they clean up
        flux:
          completeWorkers: true
        
        # This is a list because a pod can support multiple containers
        containers:
        - image: ghcr.io/converged-computing/metric-lammps:latest

          # You can set the working directory if your container WORKDIR is not correct.
          workingDir: /opt/lammps/examples/reaxff/HNS
          resources:
            limits:
              cpu: {{ cpu_limit }}
            requests:
              cpu: {{ cpu_limit }}
"""

# tasks == size * cpu_limit
experiment_setups = {
    "autoscaling": {
        "template": minicluster_template,
        "kwargs": [
            {
                "size": "2",
                "tasks": "8",
                "cpu_limit": "4",
                "x": "2",
                "y": "2",
                "z": "2",
            },
            {
                "size": "4",
                "tasks": "16",
                "cpu_limit": "4",
                "x": "2",
                "y": "2",
                "z": "2",
            },
            {
                "size": "6",
                "tasks": "24",
                "cpu_limit": "4",
                "x": "2",
                "y": "2",
                "z": "2",
            },
            {
                "size": "8",
                "tasks": "32",
                "cpu_limit": "4",
                "x": "2",
                "y": "2",
                "z": "2",
            },
        ],
    },
    # Tasks and sizes are provided in the ensemble template
    # Size is the starting size (will scale up)
    "ensemble-random": {
        "template": ensemble_template,
        "kwargs": [
            {
                "cpu_limit": "4",
                "x": "2",
                "y": "2",
                "z": "2",
                # Size here is starting size (1)
                "size": "1",
                "order": "random",
            },
        ],
    },
    "ensemble-ascending": {
        "template": ensemble_template,
        "kwargs": [
            {
                "cpu_limit": "4",
                "x": "2",
                "y": "2",
                "z": "2",
                "size": "1",
                "order": "ascending",
            },
        ],
    },
    "ensemble-descending": {
        "template": ensemble_template,
        "kwargs": [
            {
                "cpu_limit": "4",
                "x": "2",
                "y": "2",
                "z": "2",
                "size": "1",
                "order": "descending",
            },
        ],
    },
    # This mimics running all jobs in one static minicluster
    "ensemble-static": {
        "template": ensemble_template,
        "kwargs": [
            {
                "cpu_limit": "4",
                "x": "2",
                "y": "2",
                "z": "2",
                "size": "40",
                "order": "random",
            },
        ],
    },
    "static-max-size": {
        "template": minicluster_template,
        "kwargs": [
            {
                "size": "2",
                "tasks": "8",
                "cpu_limit": "4",
                "x": "2",
                "y": "2",
                "z": "2",
            },
            {
                "size": "4",
                "tasks": "16",
                "cpu_limit": "4",
                "x": "2",
                "y": "2",
                "z": "2",
            },
            {
                "size": "6",
                "tasks": "24",
                "cpu_limit": "4",
                "x": "2",
                "y": "2",
                "z": "2",
            },
            {
                "size": "8",
                "tasks": "32",
                "cpu_limit": "4",
                "x": "2",
                "y": "2",
                "z": "2",
            },
        ],
    },
}


def run_command(command, allow_fail=False):
    """
    Call a command to subprocess, return output and error.
    """
    p = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    o, e = p.communicate()
    print(o)
    if p.returncode and not allow_fail:
        print(e)
        print(f"WARNING error in subprocess: {e}")
    return o, e


def confirm_action(question):
    """
    Ask for confirmation of an action
    """
    response = input(question + " (yes/no)? ")
    while len(response) < 1 or response[0].lower().strip() not in "ynyesno":
        response = input("Please answer yes or no: ")
    if response[0].lower().strip() in "no":
        return False
    return True


def monitor_nodes(history, outdir):
    """
    Monitor nodes, to keep track of up and down time.

    Note that more metadata about different phases is in
    kubectl get nodes -o json | jq .items[].status.conditions
    """
    global cli

    # Output file to write results to
    outfile = os.path.join(outdir, "instance-activity.json")

    # Keep running, check every 15 seconds
    k8s = cli.get_k8s_client()

    # Updated list of nodes seen
    seen = set()

    # Make a count of nodes ready
    for node in k8s.list_node().items:
        name = node.metadata.name

        # If the instance isn't in our current cache, it's new!
        if name not in history:
            creation_timestamp = node.metadata.creation_timestamp
            naive = creation_timestamp.replace(tzinfo=None)
            history[name] = {"creation_time": naive}
            print(f"🥸️ New node added to cluster {name}.")
        seen.add(name)

    # Now go through cache, nodes that aren't in seen were removed
    # And we can't have seen them before
    for name in history:
        if name not in seen and "noticed_disappeared_time" not in history[name]:
            print(f"🥸️ Node {name} has dissappeared from cluster.")
            now = datetime.utcnow()
            history[name]["noticed_disappeared_time"] = str(now)

    # Save data on each pass, need to ensure we have a string
    history_saved = {}
    for name in history:
        history_saved[name] = copy.deepcopy(history[name])
        history_saved[name]["creation_time"] = str(history_saved[name]["creation_time"])
    utils.write_json(history_saved, outfile)


def get_parser():
    parser = argparse.ArgumentParser(
        description="Ensemble Experiment Running",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--data-dir",
        help="path to save data",
        default=os.path.join(here, "data"),
    )
    parser.add_argument(
        "--zone",
        help="Zone to request resources for (e.g., us-central1-a).",
        default="us-central1-a",
    )
    parser.add_argument(
        "--region",
        help="Region to request resources for (e.g., us-central1). Be careful, as this often means getting them across zones.",
    )
    parser.add_argument(
        "--iters",
        help="iterations of each experiment to run per cluster (set to 1 for testing)",
        type=int,
        default=1,
    )
    parser.add_argument(
        "--min-nodes",
        help="minimum number of nodes",
        type=int,
        default=1,
    )
    parser.add_argument(
        "--max-nodes",
        help="maximum number of nodes",
        type=int,
        default=40,
    )
    parser.add_argument(
        "--scale-down-to",
        help="size to scale down to - defaults to min size + 2 if unset",
        type=int,
        default=0,
    )
    parser.add_argument(
        "--cluster-name",
        help="cluster name to use (defaults to spot-instance-testing-cluster",
        default="ensemble-cluster",
    )
    parser.add_argument(
        "--project",
        help="Google cloud project name",
        default="llnl-flux",
    )
    parser.add_argument(
        "--machine-type",
        help="machine type for single 'sticky' node to install operators to.",
        default="c2d-standard-8",
    )
    parser.add_argument(
        "--regex",
        help="regular expression to filter experiments to run",
    )
    parser.add_argument(
        "--skip",
        help="skip these experiments",
        choices=list(experiment_setups),
        action="append",
    )
    parser.add_argument(
        "--name",
        help="select named experiments",
        choices=list(experiment_setups),
        action="append",
    )
    parser.add_argument(
        "--max-vcpu",
        help="Max vcpu per node",
        type=int,
        default=8,
    )
    parser.add_argument(
        "--skip-scale-down",
        help="Skip scaling down",
        default=False,
        action="store_true",
    )
    parser.add_argument(
        "--delete",
        help="Just delete the cluster if a script errored",
        default=False,
        action="store_true",
    )
    parser.add_argument(
        "--max-memory",
        help="Max memory for each node",
        type=int,
        default=32,
    )
    return parser


def describe_instances_topology():
    """
    Get the topology for the instance types we have chosen.

    We originally wanted to use the topology API here, but it's limited. :/
    """
    global cli
    k8s = cli.get_k8s_client()

    # We need instance ids, organized by zone (AvailabilityZone)
    instances = []
    for node in k8s.list_node().items:
        instance_type = node.metadata.labels["beta.kubernetes.io/instance-type"]
        zone = node.metadata.labels["topology.kubernetes.io/zone"]
        region = node.metadata.labels["topology.kubernetes.io/region"]
        instances.append(
            {
                "type": instance_type,
                "zone": zone,
                "region": region,
                "name": node.metadata.name,
            }
        )
    return instances


def run_kubectl(command, allow_fail=False):
    """
    Wrapper to client to run command with kubectl.

    This requires you to run the gcloud command first.
    """
    global cli
    command = f"kubectl {command}"
    print(command)
    res = os.system(command)
    if res != 0 and not allow_fail:
        print(
            f"ERROR: running {command} - debug and ensure it works before exiting from session."
        )
        import IPython

        IPython.embed()
    return res


def install_operators(allow_fail=False):
    """
    Install all needed operators to the spot cluster

    Also create the registry (just an admission webnhook) and secret for it.
    """
    global cli

    # JobSet needs to be applied server side, otherwise error about annotations
    filename = os.path.join(here, "crd", "flux-operator.yaml")
    run_kubectl(f"apply -f {filename}")
    time.sleep(5)

    # Metrics Operator
    filename = os.path.join(here, "crd", "ensemble-operator.yaml")
    run_kubectl(f"apply -f {filename}", allow_fail=allow_fail)
    time.sleep(5)
    run_kubectl("get pods --all-namespaces -o=wide")


def submit_job(minicluster_yaml):
    """
    Create the job in Kubernetes.
    """
    fd, filename = tempfile.mkstemp(suffix=".yaml", prefix="minicluster-")
    os.remove(filename)
    os.close(fd)
    utils.write_file(minicluster_yaml, filename)

    # Create the minicluster
    o, e = run_command(["kubectl", "apply", "-f", filename])
    os.remove(filename)


def delete_minicluster(uid):
    """
    Delete the Minicluster, which includes an indexed job,
    config maps, and service.
    """
    # --wait=true is default, but I want to be explicit
    run_command(
        ["kubectl", "delete", "miniclusters.flux-framework.org", uid, "--wait=true"]
    )


def submit_container_pull(args, template):
    """
    Submit a job to pull containers at the max size
    """
    print("Submitting job to pull containers")
    pull_job = {
        "size": args.max_nodes,
        "tasks": args.max_nodes * args.max_vcpu,
        "cpu_limit": "3",
        "x": "2",
        "y": "2",
        "z": "2",
        "name": "pull-job",
    }
    minicluster_yaml = template.render(pull_job)
    print(minicluster_yaml)
    submit_job(minicluster_yaml)

    # If we wait too quickly won't find any pods
    time.sleep(10)

    # Wait for all pods to be complete (completed or running is fine, means the container was pulled)
    wait_for_pods_complete(states=["Succeeded", "Running"])

    # This one we can delete - don't want in history
    delete_minicluster(pull_job["name"])

    if not args.skip_scale_down:
        wait_for_size(args.scale_down_to)

def wait_for_pods_complete(history=None, path=None, states=None):
    """
    Wait for all pods to be in completion state.

    This indicates the experiment is done running.
    """
    config.load_kube_config()
    kube_client = client.CoreV1Api()
    states = states or ["Succeeded", "Failed"]

    while True:
        if history is not None and path is not None:
            monitor_nodes(history, path)
        pods = kube_client.list_namespaced_pod(namespace="default")
        phases = [x.status.phase for x in pods.items]
        is_done = all([p in states for p in phases])
        if is_done:
            print(f"Pods are all completed: {phases}")
            return


def wait_for_size(size, history=None, path=None):
    """
    Wait for the cluster to scale down to a certain size
    """
    global cli

    print(f"Waiting for cluster to scale to size {size}")

    # Keep running, check every 15 seconds
    k8s = cli.get_k8s_client()
    start = time.time()

    # Make a count of nodes ready
    while True:
        count = len(k8s.list_node().items)
        print(f"Cluster has size {count}, waiting for size {size}")
        if history is not None and path is not None:
            monitor_nodes(history, path)
            
        # It ranges between 3 and 4
        if count <= size or count == size + 1:
            end = time.time()
            return end - start
        time.sleep(2)


def run_single_miniclusters(name, exp, args, path, pull_containers=False):
    """
    Run MiniClusters a la carte
    """
    global cli

    # Get an initial state of nodes
    history = {}

    # Minicluster template
    template = Template(minicluster_template)

    # A new result object for each. Runs results go into the registry
    results = []
    for i in range(args.iters):
        result = {
            "name": name,
            "times": cli.times,
            "cluster_name": args.cluster_name,
            "metadata": exp,
            "machine_type": args.machine_type,
        }

        # Get a starting topology
        topology = describe_instances_topology()
        result["topology"] = topology

        # Generate jobs to run from kwargs, also make a short service name
        jobs = []
        count = 0
        for cfg in exp["kwargs"]:
            for i in range(0, 3):
                size = cfg["size"]
                xyz = f'{cfg["x"]}-{cfg["y"]}-{cfg["z"]}'
                service_name = f"s{count}"
                # uid is the iteration, size, x,y,z and scheduler
                uid = f"lmp-{i}-{count}-size-{size}-{xyz}"
                job = copy.deepcopy(cfg)
                job["name"] = uid
                job["service_name"] = service_name
                jobs.append(job)
                count += 1

        # Submit a max size job to pull to minicluster
        if pull_containers:
            submit_container_pull(args, template)
        monitor_nodes(history, path)

        # Randomly shuffle them
        random.shuffle(jobs)

        # Submit each one!
        for job in jobs:
            print(f"Submitting job {job['name']}: {job}")
            render = copy.deepcopy(job)

            # TODO: what else do we want to save here?
            result["params"] = render
            result["iter"] = i

            # Generate and submit the template...
            minicluster_yaml = template.render(render)
            print(minicluster_yaml)

            # This submits the job, doesn't do more than that (e.g., waiting)
            submit_job(minicluster_yaml)
            monitor_nodes(history, path)
            submit_time = datetime.utcnow()
            result["submit_time"] = str(submit_time)
            results.append(result)

    # This waits for all pods to complete and records history events (nodes added and removed)
    wait_for_pods_complete(history, path)
    all_complete = datetime.utcnow()

    # Get pod info first - the completed pods will go away
    pod_info = get_pod_info()

    # Then we wait for the cluster to scale back down to the min size
    # But add 2 to account for operators installed
    scale_down_seconds = 0
    if not args.skip_scale_down:
        scale_down_seconds = wait_for_size(args.scale_down_to, history, path)

    # One more call to save history
    monitor_nodes(history, path)

    # When they are done, get the metadata about pod times for each.
    # It's easier to save everything - we will need to know the pods run on and times.
    final = {
        "jobs": results,
        "scale_down_min_size_seconds": scale_down_seconds,
        "pods": pod_info,
        "completed": str(all_complete),
    }
    outfile = os.path.join(path, "results.json")
    utils.write_json(final, outfile)

    # Clean up miniclusters (configs, services, etc).
    for job in jobs:
        delete_minicluster(job["name"])
    return final


def get_namespace_logs(namespace, container):
    """
    Get logs for all pods in a namespace.

    This doesn't watch (stream) but assumes we are done.
    """
    config.load_kube_config()
    kube_client = client.CoreV1Api()

    logs = {}
    pods = kube_client.list_namespaced_pod(namespace=namespace)
    for pod in pods.items:
        lines = kube_client.read_namespaced_pod_log(
            name=pod.metadata.name, namespace=namespace, container=container
        )
        logs[pod.metadata.name] = lines.split("\n")

    # This gets huge otherwise
    return logs


def run_ensemble(name, exp, args, path, pull_containers=False):
    """
    Run the ensemble operator
    """
    global cli

    # Get an initial state of nodes
    history = {}

    # Ensemble and Minicluster template (for pull)
    template = Template(ensemble_template)

    # Minicluster template
    mc_template = Template(minicluster_template)
    if pull_containers:
        submit_container_pull(args, mc_template)
    monitor_nodes(history, path)

    # A new result object for each. Runs results go into the registry
    results = []
    for i in range(args.iters):
        result = {
            "name": name,
            "times": cli.times,
            "cluster_name": args.cluster_name,
            "metadata": exp,
            "machine_type": args.machine_type,
        }

        # Get a starting topology
        topology = describe_instances_topology()
        result["topology"] = topology

        # Submit the ensemble
        # For the ensemble, they are run internally on the same
        # minicluster so we don't need to run a gazillion jobs
        for count, cfg in enumerate(exp["kwargs"]):
            uid = f"ensemble-{count}"
            print(f"Submitting job {uid}: {cfg}")
            render = copy.deepcopy(cfg)
            render.update(
                {"name": uid, "max_size": args.max_nodes, "min_size": args.min_nodes}
            )

            # TODO: what else do we want to save here?
            result["params"] = render
            result["iter"] = i

            # Generate and submit the template...
            ensemble_yaml = template.render(render)
            print(ensemble_yaml)

            # This submits the job, doesn't do more than that (e.g., waiting)
            submit_job(ensemble_yaml)
            submit_time = datetime.utcnow()
            monitor_nodes(history, path)
            result["submit_time"] = str(submit_time)
            results.append(result)

    # This waits for all pods to complete and records history events (nodes added and removed)
    # This is a slightly different design because the entire ensemble is one minicluster!
    wait_for_pods_complete(history, path)
    all_complete = datetime.utcnow()

    print("Getting finished logs for manager")

    # Get complete logs for ensemble operator - this has the json dump of flux output
    logs = get_namespace_logs("ensemble-operator-system", container="manager")

    # Then we wait for the cluster to scale back down to the min size
    # But add 2 to account for operators installed
    scale_down_seconds = 0
    if not args.skip_scale_down:
        scale_down_seconds = wait_for_size(args.scale_down_to, history, path)

    # One more call to save history
    monitor_nodes(history, path)

    # One more call to get when scaled down
    scaled_down = datetime.utcnow()

    # When they are done, get the metadata about pod times for each.
    # It's easier to save everything - we will need to know the pods run on and times.
    final = {
        "jobs": results,
        "scale_down_min_size_seconds": scale_down_seconds,
        "logs": logs,
        "completed": str(all_complete),
        "scaled_down": str(scaled_down),
    }
    outfile = os.path.join(path, "results.json")
    utils.write_json(final, outfile)
    return final


def get_pod_info():
    """
    Get pod info
    """
    config.load_kube_config()
    kube_client = client.CoreV1Api()
    pods = kube_client.list_namespaced_pod(namespace="default", _preload_content=False)
    return json.loads(pods.data)


def main():
    """
    Run experiments
    """
    parser = get_parser()

    # If an error occurs while parsing the arguments, the interpreter will exit with value 2
    args, _ = parser.parse_known_args()
    if not os.path.exists(args.data_dir):
        os.makedirs(args.data_dir)

    # If the scale down size is unset, default to min size +2
    if args.scale_down_to == 0:
        args.scale_down_to = args.min_nodes + 2

    # Only one of the zone or region is allowed
    if args.zone and args.region:
        sys.exit("You must select a single --zone OR --region.")
    location = args.zone or args.region

    print("🪴️ Planning to run:")
    print(f"   Project             : {args.project}")
    print(f"   Cluster name        : {args.cluster_name}")
    print(f"     delete            : {args.delete}")
    print(f"     location          : {location}")
    print(f"     min_size          : {args.min_nodes}")
    print(f"     max_size          : {args.max_nodes}")
    print(f"     scale_down        : {args.scale_down_to}")
    print(f"       skipped         : {args.skip_scale_down}")
    print(f"   Output Data         : {args.data_dir}")
    print(f"   Experiments         : {len(experiment_setups)}")
    print(f"     iterations        : {args.iters}")
    print(f"     machine-type      : {args.machine_type}")

    # Skip takes preference over names
    names = args.name
    if not names:
        names = list(experiment_setups)

    # Do we have a regular expression?
    if args.regex is not None:
        updated = []
        for name in names:
            if re.search(args.regex, name):
                updated.append(name)
        names = updated

    updated = []
    for name in names:
        if args.skip is not None and name in args.skip:            
            continue
        updated.append(name)
    names = updated

    print("🧪️ Experiments:")
    for name in names:
        print(f"   ==> {name}")

    print()
    if not confirm_action("Would you like to continue?"):
        sys.exit("📺️ Cancelled!")

    # Main experiment running, show total time just for user FYI
    start_experiments = time.time()
    run_experiments(experiment_setups, names, args)
    stop_experiments = time.time()
    total = stop_experiments - start_experiments
    print(f"total time to run is {total} seconds")


def run_experiments(experiments, names, args):
    """
    Wrap experiment running separately in case we lose spot nodes and can recover
    """
    global cli

    # Use kubescaler gkecluster to create the cluster - min to max size (1 to 8)
    cli = GKECluster(
        project=args.project,
        machine_type=args.machine_type,
        name=args.cluster_name,
        # The client will generalize this to location (and we expect one or the other)
        # preference is given to zone, as it is more specific
        region=args.region,
        zone=args.zone,
        node_count=args.max_nodes,
        min_nodes=args.min_nodes,
        max_nodes=args.max_nodes,
        max_vcpu=args.max_vcpu,
        max_memory=args.max_memory,
        # This is the most aggressive to add/remove nodes -
        # the other profile "balanced" is really slow to respond
        scaling_profile=1,
    )

    # We just have a request to delete
    if args.delete:
        print("🤓️ Responding to delete request!")
        cli.delete_cluster()
        sys.exit()

    # This times the cluster creation
    # If your script errors you can comment this out and run again.
    cli.create_cluster()

    # This is cheating a bit, I couldn't get the cert manager installed
    res = os.system(
        f"gcloud container clusters get-credentials {cli.cluster_name} --location={cli.location}"
    )
    if res != 0:
        print("Issue getting kube config credentials, debug!")
        import IPython

        IPython.embed()

    # Install the Flux Operator and Ensemble Operator (we need both)
    start = time.time()
    install_operators()
    end = time.time()
    cli.times["install_operators"] = end - start

    # Save original times to reset each experiment
    original_times = copy.deepcopy(cli.times)
    completed_times = {}

    # Note that the experiment already has a table of values filtered down
    # Each experiment has some number of batches (we will typically just run one experiment)
    count = 0
    for name, exp in experiments.items():
        # Selectively skip or run experiments
        if name not in names:
            continue

        print(f"== Preparing to run experiment {name} 🧪️")

        # Each experiment starts only with cluster creation times
        cli.times = copy.deepcopy(original_times)

        # Create an output directory for each experiment
        path = os.path.join(args.data_dir, name)
        if not os.path.exists(path):
            os.makedirs(path)

        # TODO update the cluster to ensure we are at the right starting size
        is_ensemble = "ensemble" in exp["template"].lower()

        # Pull containers on the first
        pull_containers = count == 0

        # If we are on the last run, we need to adjust the size to
        # be fixed on the max size. Note that I tested this, and the API
        # calls works but I didn't see the pool scale so I'm not using it
        static = exp.get("static")
        if static is True:
            print(f"Upgrading cluster to static size of max nodes: {args.max_nodes}")
            cli.update_cluster(args.max_nodes, args.max_nodes, args.max_nodes)

        # If it's not an ensemble, we submit the batch at once
        # Note that the miniclusters are first, so the ensemble should
        # not need to pull
        if not is_ensemble:
            run_single_miniclusters(
                name, exp, args, path, pull_containers=pull_containers
            )
        else:
            run_ensemble(name, exp, args, path, pull_containers=pull_containers)
        cli.times["experiment_finished"] = str(datetime.utcnow())

        # Save client and experiment finished times
        completed_times[name] = {
            "path": os.path.join(path, "client-times.json"),
            "times": copy.deepcopy(cli.times),
        }
        # Write data as we go in case we lose the cluster mid experiments
        utils.write_json(completed_times[name]["times"], completed_times[name]["path"])
        count += 1

    watcher.stop()
    print(f"🧪️ Experiments are finished. See output in {args.data_dir}")

    # When we are done, delete the entire cluster
    # I hope this includes node groups, we will see
    cli.delete_cluster()
    for experiment, meta in completed_times.items():
        meta["times"]["delete_cluster"] = cli.times["delete_cluster"]
        utils.write_json(meta["times"], meta["path"])


if __name__ == "__main__":
    main()
