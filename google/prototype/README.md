# Workload Demand Experiment

You can read about the workload demand algorithm [here](https://github.com/converged-computing/ensemble-operator/blob/main/docs/algorithms.md#workoad-demand-of-consistent-sizes). Here we are doing a small experiment to test the following cases:

- static base case without ensemble (launching separate Miniclusters for each job) at max size
- autoscaling base case without ensemble (launching separate Miniclusters for each job) starting at min size, allowing scale up
- workload driven ensemble with autoscaler enabled and different submit approaches
  - random submit
  - ascending job size
  - descending job size

Note that there are two [autoscaling profiles](https://cloud.google.com/kubernetes-engine/docs/concepts/cluster-autoscaler#autoscaling_profiles) balanced (default) and optimize-utilization. I first tested balanced and found
that nodes hung around ~10 minutes after the queue was essentially empty, so I think the second one (that is noted to be more
aggressive) might be better. We are going to (as an extra bonus) keep track of the time the cluster takes to go back to the smallest
size when no work is running. I didn't see this was a parameter I could update. Note that we are explicitly choosing even sizes because we are hoping to not clog. Fingers crossed.

## 1. Create the Cluster

These experiments are all run and controlled with python.

```bash
python run_experiments.py --help

# These are two different setups - the static is one consistent size
python run_experiments.py --data-dir ./data/run5 --skip static-max-size --skip ensemble-static
```
```console
Cluster has size 6, waiting for size 3
Cluster has size 3, waiting for size 3
🥸️ Node gke-ensemble-cluster-default-pool-69242275-zzhz has dissappeared from cluster.
🥸️ Node gke-ensemble-cluster-default-pool-69242275-ck4q has dissappeared from cluster.
🥸️ Node gke-ensemble-cluster-default-pool-69242275-pdq6 has dissappeared from cluster.
🧪️ Experiments are finished. See output in ./data/run5
total time to run is 2778.8086202144623 seconds
```
```bash
python run_experiments.py --data-dir ./data/run5 --name static-max-size --name ensemble-static --min-nodes=24 --max-nodes=24 --skip-scale-down
```
```console
minicluster.flux-framework.org "lmp-0-9-size-8-2-2-2" deleted

🧪️ Experiments are finished. See output in ./data/run5
total time to run is 1158.910121679306 seconds
```

If something borks and you just need to delete the cluster:

```bash
python run_experiments.py --delete
```

## 2. Plot Results

Note that I have a few of these because I was working on tweaking / improving them incrementally.

```bash
python plot-results.py --out ./img/run1 --results ./data/run1
python plot-results.py --out ./img/run2 --results ./data/run2
python plot-results.py --out ./img/run3 --results ./data/run3
python plot-results.py --out ./img/run4 --results ./data/run4
python plot-results.py --out ./img/run5 --results ./data/run5
```

Right now the web UI is a bit hard coded, and generates from json data. I am planning to refactor this into more of a Jekyll template (that can have multiple data inputs for different experimetns) eventually.
