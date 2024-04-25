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

**Important** if you change the max size of the cluster, the min size likely changes too. For size 24 I was allowed to go down to 3. For size 40 I had to stay at 4.

## 1. Create the Cluster

These experiments are all run and controlled with python.

```bash
# Skip static here, which needs to start at size 40
python run_experiments.py --data-dir ./data/run0 --regex ensemble --skip ensemble-static --max-nodes=40
python run_experiments.py --data-dir ./data/run1 --regex ensemble --skip ensemble-static --max-nodes=40

# For static we start at size 40 and stay at size 40
python run_experiments.py --data-dir ./data/run0 --name ensemble-static --max-nodes=40 --skip-scale-down
```

If something borks and you just need to delete the cluster:

```bash
python run_experiments.py --delete
```

## 2. Plot Results

Note that I have a few of these because I was working on tweaking / improving them incrementally.

```bash
python plot-results.py
```
