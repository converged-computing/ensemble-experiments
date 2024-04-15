#!/usr/bin/env python3

import argparse
import collections
import fnmatch
import json
import os

from datetime import datetime, timedelta
import matplotlib.pyplot as plt
import kubescaler.utils as utils
import pandas
import seaborn as sns

"""
Each experiment result should be in this format
[
    {
        "id": "experiment",
        "title": "Experiment Name",
        "data": {
            "chart_options": { "vaccine_year": 1967.5 },
            "values": {
                "data": [ 
                   [<time-interval>, <node-index>, <pods-running>]
                ]
...
]
"""

# Keep a global node lookup so between experiments
# they mean the same thing
node_lookup = {}
node_counter = 0

# Experiment interval lengths to compare
experiment_intervals = {}

plt.style.use("bmh")
here = os.path.dirname(os.path.abspath(__file__))


def get_parser():
    parser = argparse.ArgumentParser(
        description="Plot Ensemble Results",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--results",
        help="directory with raw results data",
        default=os.path.join(here, "data", "run0"),
    )
    parser.add_argument(
        "--out",
        help="directory to save parsed results",
        default=os.path.join(here, "img"),
    )
    return parser


def recursive_find(base, pattern="*.*"):
    """
    Recursively find and yield files matching a glob pattern.
    """
    for root, _, filenames in os.walk(base):
        for filename in fnmatch.filter(filenames, pattern):
            yield os.path.join(root, filename)


def main():
    """
    Run the main plotting operation!
    """
    parser = get_parser()
    args, _ = parser.parse_known_args()

    # Output images and data
    outdir = os.path.abspath(args.out)
    indir = os.path.abspath(args.results)
    if not os.path.exists(outdir):
        os.makedirs(outdir)

    # Find input results
    names = os.listdir(indir)
    data = []
    pods_dfs = []
    all_dfs = {}

    # Save a master pods_df
    for name in names:
        print(f"Parsing experiment {name}")
        new_data, dfs = parse_experiment(indir, name)
        data += new_data
        all_dfs[name] = {}
        pods_dfs.append(dfs["pods"])
        for df_name, df in dfs.items():
            df.to_csv(os.path.join(outdir, f"{name}-{df_name}.csv"))
            all_dfs[name][df_name] = df

    pods_df = pandas.concat(pods_dfs)
    all_dfs["pods"] = pods_df
    pods_df.to_csv(os.path.join(outdir, "all-experiment-pods.csv"))

    # Save data to file
    utils.write_json(data, os.path.join(outdir, "web-results.json"))
    plot_results(all_dfs, outdir)

    # We need to update nodes in the plot
    print([str(x) for x in list(range(0, node_counter))])
    print(json.dumps(experiment_intervals, indent=4))


def date_range(start, end, intv, name):
    """
    Generate a range of dates from the string intervals

    We might want to remove the Year, month, day
    """
    global experiment_intervals

    start = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
    end = datetime.strptime(end, "%Y-%m-%d %H:%M:%S")

    # This is the interval length
    diff = (end - start) / intv
    experiment_intervals[name] = diff.seconds

    intervals = []
    for i in range(intv):
        intervals.append((start + diff * i).strftime("%Y-%m-%d %H:%M:%S"))
    intervals.append(end.strftime("%Y-%m-%d %H:%M:%S"))
    return intervals


def parse_experiment(indir, name):
    """
    Parse an experiment results
    """
    global node_lookup
    global node_counter

    results_json = os.path.join(indir, name, "results.json")
    times_json = os.path.join(indir, name, "client-times.json")
    nodes_json = os.path.join(indir, name, "instance-activity.json")
    nodes = utils.read_json(nodes_json)
    times = utils.read_json(times_json)
    exp_results = utils.read_json(results_json)

    # Create a data frame of node up / down / elapsed times
    df = pandas.DataFrame(columns=["node", "creation_time", "noticed_disappeared_time"])
    idx = 0
    for node, meta in nodes.items():
        df.loc[idx, :] = [
            node,
            meta["creation_time"],
            meta.get("noticed_disappeared_time"),
        ]
        idx += 1

    # The disappeared times that are NaT coincide with the deletion of the cluster
    # TODO this likely needs to be updated to the latest time seen
    df.noticed_disappeared_time = df.noticed_disappeared_time.fillna(
        times["experiment_finished"]
    )

    # Calculate elapsed times in seconds
    elapsed = []
    cts = df.creation_time.tolist()
    for i, t in enumerate(df.noticed_disappeared_time.tolist()):
        ct = datetime.strptime(str(cts[i]), "%Y-%m-%d %H:%M:%S")
        t = datetime.strptime(str(t).split(".")[0], "%Y-%m-%d %H:%M:%S")
        elapsed.append((t - ct).seconds)
    df["elapsed_time"] = elapsed

    # Sort by creation time
    df = df.sort_values("creation_time")
    df.creation_time = pandas.to_datetime(df.creation_time)
    df.noticed_disappeared_time = pandas.to_datetime(df.noticed_disappeared_time)

    # Now we need to break into N intervals, like:
    # [<time-interval>, <node-index>, <active>]
    # Add some padding to each
    earliest_time = df.creation_time.min()
    latest_time = df.noticed_disappeared_time.max()

    print(df)
    # Latest time will have microseconds, but K8s doesn't have that
    latest_time = str(latest_time).rsplit(".", 1)[0]
    intervals = date_range(str(earliest_time), latest_time, 35, name)

    # Now create unique nodes (and their index)
    node_names = df.node.sort_values().unique()
    for node_name in node_names:
        if node_name not in node_lookup:
            node_lookup[node_name] = {"index": node_counter}
            node_counter += 1

    # Now we need to keep, for each interval, the specific nodes active
    # The GUI uses the index instead of the node name. I think observable
    # wants epoch milliseconds
    active = []
    added = set()
    for i, interval in enumerate(intervals):
        start_interval_dt = datetime.strptime(interval, "%Y-%m-%d %H:%M:%S")
        for node in node_names:
            end_interval_dt = None
            if i < len(intervals) - 1:
                end_interval_dt = datetime.strptime(
                    intervals[i + 1], "%Y-%m-%d %H:%M:%S"
                )
            else:
                # This just gives enough padding to define
                end_interval_dt = df[df.node == node].noticed_disappeared_time.tolist()[
                    0
                ] + timedelta(minutes=1)

            # For now I'm appending the interval id since I don't know how to parse
            # date time correctly in javascript, derp
            ct = df[df.node == node].creation_time.tolist()[0]
            et = df[df.node == node].noticed_disappeared_time.tolist()[0]
            if (
                ct < start_interval_dt
                and end_interval_dt is not None
                and et >= end_interval_dt
            ):
                active.append([i, node_lookup[node]["index"], 1])
                added.add(node)

            # We do not append if the node was not active
    print(f"Adding {len(added)} nodes as active for experiment {name}")

    # Make epoch times for when the experiment ended (pods cleand up) vs jobs done
    finished_dt = datetime.strptime(
        times["experiment_finished"].split(".")[0], "%Y-%m-%d %H:%M:%S"
    )
    jobs_done_dt = datetime.strptime(
        exp_results["completed"].split(".")[0], "%Y-%m-%d %H:%M:%S"
    )

    dfs = {"nodes": df}

    # Figure out the intervals when the pods were done vs the experiment done (nodes back to 3)
    jobs_interval = None
    finished_interval = None
    for i, interval in enumerate(intervals):
        start_interval_dt = datetime.strptime(interval, "%Y-%m-%d %H:%M:%S")
        # The latest interval it happens after
        if jobs_interval is None or jobs_done_dt > start_interval_dt:
            jobs_interval = i
        if finished_interval is None or finished_dt > start_interval_dt:
            finished_interval = i

    # Prepare in format we need for result
    results = [
        {
            "id": name,
            "title": "Experiment "
            + " ".join([x.capitalize() for x in name.split("-")]),
            "data": {
                "chart_options": {
                    "jobs_finished": jobs_interval,
                    "experiment_finished": finished_interval,
                },
                "values": {"data": active},
            },
        }
    ]

    # Make an equivalent data frame with pods waiting times (we only have these for the non-ensemble runs)
    if "pods" in exp_results:
        dfs["pods"] = get_pods_timing(exp_results, name)

    # This is the flux queue jobs dump - note, not used yet, durations are not set
    else:
        jobs = [x for x in list(exp_results["logs"].values())[0] if '{"jobs"' in x]
        if jobs:
            jobs = json.loads(jobs[-1])
            dfs["pods"] = get_jobs_timing(jobs, name)

    return results, dfs


def get_jobs_timing(jobs, name):
    """
    This isn't comparing apples to apples, but it's the granularity we have
    for flux jobs. Likely we could monitor the pods too.

    For ensemble, we instead have the job queue
    """
    jobs_df = pandas.DataFrame(
        columns=[
            "pod",
            "job",
            "size",
            "start_time",
            "finished_time",
            "elapsed_time",
            "experiment",
        ]
    )
    idx = 0
    for job in jobs["jobs"]:
        jobs_df.loc[idx, :] = [
            job["nodelist"],
            job["id"],
            job["nnodes"],
            job["t_submit"],
            None,
            job["t_cleanup"] - job['t_run'],
            name,
        ]
        idx += 1
    print(jobs_df)
    return jobs_df


def get_pods_timing(exp_results, name):
    """
    Given minicluster runs, we have complete metadata for pods.

    For ensemble, we instead have the job queue
    """
    pods_df = pandas.DataFrame(
        columns=[
            "pod",
            "job",
            "size",
            "start_time",
            "finished_time",
            "elapsed_time",
            "experiment",
        ]
    )
    idx = 0
    for pod in exp_results["pods"]["items"]:
        job = "-".join(pod["metadata"]["name"].split("-")[:-2])
        size = int(job.split("-")[4])
        finished_ts = pod["status"]["containerStatuses"][0]["state"]["terminated"][
            "finishedAt"
        ]
        start_ts = pod["status"]["startTime"]

        # Calculate elapsed time in seconds
        st = datetime.strptime(str(start_ts), "%Y-%m-%dT%H:%M:%SZ")
        et = datetime.strptime(str(finished_ts), "%Y-%m-%dT%H:%M:%SZ")
        elapsed_seconds = (et - st).seconds

        pods_df.loc[idx, :] = [
            pod["metadata"]["name"],
            job,
            size,
            start_ts,
            finished_ts,
            elapsed_seconds,
            name,
        ]
        idx += 1
    return pods_df


def plot_results(dfs, outdir):
    """
    Plot lammps results
    """
    # Separate pods df
    pods_df = dfs["pods"]
    del dfs["pods"]

    # Make one master data frame for all experiments
    df = pandas.DataFrame(columns=["node", "experiment", "elapsed"])
    idx = 0
    for experiment, res in dfs.items():
        for row in res["nodes"].iterrows():
            if row[1].elapsed_time is None:
                continue
            df.loc[idx, :] = [row[1].node, experiment, row[1].elapsed_time]
            idx += 1

    # Plot each - this first one is the node elapsed time per experiment
    colors = sns.color_palette("hls", 16)
    hexcolors = colors.as_hex()
    types = list(df.experiment.unique())
    types.sort()

    # ALWAYS double check this ordering, this
    # is almost always wrong and the colors are messed up
    palette = collections.OrderedDict()
    for t in types:
        palette[t] = hexcolors.pop(0)

    make_plot(
        df,
        title="Node Up Times for Different Ensemble Experiments",
        tag="ensemble-node-times",
        ydimension="elapsed",
        xdimension="experiment",
        palette=palette,
        outdir=outdir,
        ext="png",
        plotname="ensemble-node-times",
        hue="experiment",
        plot_type="box",
        xlabel="Experiment",
        ylabel="Time (seconds)",
    )

    # Now let's look at the number of nodes per experiment type
    # Also accumulated cost time
    total_times = {}
    count_df = pandas.DataFrame(columns=["experiment", "node_count"])
    idx = 0
    for exp in df.experiment.unique():
        count = df[df.experiment == exp].shape[0]
        if exp not in total_times:
            total_times[exp] = 0
        total_times[exp] += df[df.experiment == exp].elapsed.sum()
        count_df.loc[idx, :] = [exp, count]
        idx += 1

    print("Total times for experiments:")
    print(json.dumps(total_times))
    times_df = pandas.DataFrame(columns=["experiment", "total_seconds"])
    idx = 0
    for exp, seconds in total_times.items():
        times_df.loc[idx, :] = [exp, seconds]
        idx += 1
    count_df = count_df.sort_values("node_count")
    times_df = times_df.sort_values("total_seconds")

    make_plot(
        count_df,
        title="Number of Unique Nodes per Experiment",
        tag="ensemble-unique-nodes",
        ydimension="node_count",
        xdimension="experiment",
        palette=palette,
        outdir=outdir,
        ext="png",
        plotname="ensemble-unique-nodes",
        hue="experiment",
        plot_type="bar",
        xlabel="Experiment",
        ylabel="Node Count",
    )

    make_plot(
        times_df,
        title="Total Accumulated Node Time (cost) Per Experiment",
        tag="ensemble-total-node-time",
        ydimension="total_seconds",
        xdimension="experiment",
        palette=palette,
        outdir=outdir,
        ext="png",
        plotname="ensemble-total-node-time",
        hue="experiment",
        plot_type="bar",
        xlabel="Experiment",
        ylabel="Time (seconds)",
    )

    # Now make plots for pods!
    make_plot(
        pods_df,
        title="Pod Elapsed Times for Different Ensemble Experiments",
        tag="pod-elapsed-times",
        ydimension="elapsed_time",
        xdimension="experiment",
        palette=palette,
        outdir=outdir,
        ext="png",
        plotname="pod-elapsed-times",
        hue="experiment",
        plot_type="box",
        xlabel="Experiment",
        ylabel="Time (seconds)",
    )

    # Plot each - this first one is the node elapsed time per experiment
    colors = sns.color_palette("hls", 16)
    hexcolors = colors.as_hex()
    types = list(pods_df["size"].unique())
    types.sort()

    # ALWAYS double check this ordering, this
    # is almost always wrong and the colors are messed up
    palette = collections.OrderedDict()
    for t in types:
        palette[t] = hexcolors.pop(0)

    make_plot(
        pods_df,
        title="Pods Elapsed Times (by Group Size) for Different Ensemble Experiments",
        tag="pod-elapsed-times-by-size",
        ydimension="elapsed_time",
        xdimension="experiment",
        palette=palette,
        outdir=outdir,
        ext="png",
        plotname="pod-elapsed-times-by-size",
        hue="size",
        plot_type="box",
        xlabel="Experiment",
        ylabel="Time (seconds)",
    )


def make_plot(
    df,
    title,
    tag,
    ydimension,
    xdimension,
    palette,
    xlabel,
    ylabel,
    ext="pdf",
    plotname="lammps",
    plot_type="violin",
    hue="experiment",
    outdir="img",
):
    """
    Helper function to make common plots.
    """
    plotfunc = sns.boxplot
    if plot_type == "violin":
        plotfunc = sns.violinplot
    elif plot_type == "bar":
        plotfunc = sns.barplot

    ext = ext.strip(".")
    plt.figure(figsize=(20, 12))
    sns.set_style("dark")
    if plot_type in ["violin", "bar"]:
        ax = plotfunc(x=xdimension, y=ydimension, hue=hue, data=df, palette=palette)
    else:
        ax = plotfunc(
            x=xdimension, y=ydimension, hue=hue, data=df, whis=[5, 95], palette=palette
        )
    plt.title(title)
    ax.set_xlabel(xlabel, fontsize=16)
    ax.set_ylabel(ylabel, fontsize=16)
    ax.set_xticklabels(ax.get_xmajorticklabels(), fontsize=14)
    ax.set_yticklabels(ax.get_yticks(), fontsize=14)
    plt.savefig(os.path.join(outdir, f"{tag}_{plotname}.{ext}"))
    plt.clf()


if __name__ == "__main__":
    main()
