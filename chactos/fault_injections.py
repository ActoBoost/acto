import multiprocessing
import multiprocessing.queues
import os
import queue
import subprocess
import threading
import time
from typing import Optional, Tuple

import kubernetes

from acto.common import kubernetes_client, print_event
from acto.deploy import Deploy
from acto.kubectl_client.kubectl import KubectlClient
from acto.kubernetes_engine import kind
from acto.lib.operator_config import OperatorConfig
from acto.post_process import post_diff_test
from acto.post_process.post_process import PostProcessor
from acto.result import (
    DifferentialOracleResult,
    OracleResult,
    OracleResults,
    RunResult,
    StepID,
    check_kubectl_cli,
)
from acto.runner.runner import Runner
from acto.system_state.kubernetes_system_state import KubernetesSystemState
from acto.trial import Trial
from acto.utils import acto_timer, thread_logger
from chactos.failures.failure import Failure
from chactos.failures.network_chaos import OperatorApplicationPartitionFailure
from chactos.failures.pod_failures_chaos import PodFailure
from chactos.fault_injection_config import FaultInjectionConfig, FaultType
from chactos.fault_injector import ChaosMeshFaultInjector


class ChactosDriver(PostProcessor):
    """Fault injection driver"""

    def __init__(
        self,
        testrun_dir: str,
        work_dir: str,
        operator_config: OperatorConfig,
        fault_injection_config: FaultInjectionConfig,
        num_workers: int,
    ):
        super().__init__(testrun_dir=testrun_dir, config=operator_config)
        self._operator_config = operator_config
        self._fault_injection_config = fault_injection_config
        self._work_dir = work_dir
        self._testrun_dir = testrun_dir
        self._num_workers = num_workers
        self._pod_failure_ratio = fault_injection_config.pod_failure_ratio

        self.kubernetes_provider = kind.Kind(
            acto_namespace=0,
            feature_gates=operator_config.kubernetes_engine.feature_gates,
            num_nodes=operator_config.num_nodes,
            version=operator_config.kubernetes_version,
        )

        # Build an archive to be preloaded
        container_tool = os.getenv("IMAGE_TOOL", "docker")
        self._images_archive = os.path.join(work_dir, "images.tar")
        if len(self.context["preload_images"]) > 0:
            print_event("Preparing required images...")
            # first make sure images are present locally
            for image in self.context["preload_images"]:
                subprocess.run(
                    [container_tool, "pull", image],
                    stdout=subprocess.DEVNULL,
                    check=True,
                )
            subprocess.run(
                [container_tool, "image", "save", "-o", self._images_archive]
                + list(self.context["preload_images"]),
                stdout=subprocess.DEVNULL,
                check=True,
            )

        self._deployer = Deploy(operator_config.deploy)

    def run(self) -> None:
        """Run the fault injection exp"""
        logger = thread_logger.get_thread_logger()
        logger.info("Starting fault injection exp")
        operator_selector = self._fault_injection_config.operator_selector
        operator_selector["namespaces"] = [self.context["namespace"]]
        pod_selector = self._fault_injection_config.application_selector
        pod_selector["namespaces"] = [self.context["namespace"]]

        failures: list[Tuple[FaultType, float]] = []

        # TODO: failing minority pods that fits the app_selector criteria
        logger.info("Adding pod failure to failure list")

        failures.append((FaultType.POD_FAILURE, self._pod_failure_ratio))

        logger.debug("Trials: [%s]", self.trials)
        logger.debug("Initializing runner list")
        workers: list[ChactosTrialWorker] = []

        workqueue: queue.Queue = queue.Queue()
        for failure in failures:
            for trial_name, trial in self.trial_to_steps.items():
                workqueue.put((trial_name, trial, failure))

        for worker_id in range(self._num_workers):
            worker = ChactosTrialWorker(
                worker_id=worker_id,
                work_dir=self._work_dir,
                workqueue=workqueue,
                kubernetes_provider=self.kubernetes_provider,
                image_archive=self._images_archive,
                deployer=self._deployer,
                context=self.context,
                diff_exclude_paths=self._operator_config.diff_ignore_fields,
                operator_selector=operator_selector,
                priority_pod_selector=self._fault_injection_config.priority_application_selector,
                pod_selector=pod_selector,
            )
            workers.append(worker)

        logger.debug("Launching processes")
        processes = []
        for worker in workers:
            p = threading.Thread(target=worker.run)
            p.start()
            processes.append(p)

        for p in processes:
            p.join()


def wait_for_converge(api_client, namespace, wait_time=60, hard_timeout=600):
    """This function blocks until the system converges. It keeps
        watching for incoming events. If there is no event within
        60 seconds, the system is considered to have converged.

    Args:
        hard_timeout: the maximal wait time for system convergence

    Returns:
        True if the system converge within the hard timeout
    """
    logger = thread_logger.get_thread_logger()

    start_timestamp = time.time()
    logger.info("Waiting for system to converge... ")

    core_v1_api = kubernetes.client.CoreV1Api(api_client)
    event_stream = core_v1_api.list_namespaced_event(
        namespace, _preload_content=False, watch=True
    )

    combined_event_queue = multiprocessing.Queue(maxsize=0)
    timer_hard_timeout = acto_timer.ActoTimer(
        hard_timeout, combined_event_queue, "timeout"
    )
    watch_process = multiprocessing.Process(
        target=watch_system_events,
        args=(event_stream, combined_event_queue),
    )

    timer_hard_timeout.start()
    watch_process.start()

    converge = True
    while True:
        try:
            event = combined_event_queue.get(timeout=wait_time)
            if event == "timeout":
                converge = False
                break
        except queue.Empty:
            ready = True
            app_v1_api = kubernetes.client.AppsV1Api(api_client)
            statefulsets = [
                i.to_dict()
                for i in app_v1_api.list_namespaced_stateful_set(
                    namespace
                ).items
            ]
            deployments = [
                i.to_dict()
                for i in app_v1_api.list_namespaced_deployment(namespace).items
            ]
            daemonsets = [
                i.to_dict()
                for i in app_v1_api.list_namespaced_daemon_set(namespace).items
            ]

            for sfs in statefulsets:
                if (
                    sfs["status"]["ready_replicas"] is None
                    and sfs["status"]["replicas"] == 0
                ):
                    # replicas could be 0
                    continue
                if sfs["status"]["replicas"] != sfs["status"]["ready_replicas"]:
                    ready = False
                    logger.info(
                        "Statefulset %s is not ready yet",
                        sfs["metadata"]["name"],
                    )
                    break
                if sfs["spec"]["replicas"] != sfs["status"]["replicas"]:
                    ready = False
                    logger.info(
                        "Statefulset %s is not ready yet",
                        sfs["metadata"]["name"],
                    )
                    break

            for dp in deployments:
                if dp["spec"]["replicas"] == 0:
                    continue

                if dp["status"]["replicas"] != dp["status"]["ready_replicas"]:
                    ready = False
                    logger.info(
                        "Deployment %s is not ready yet",
                        dp["metadata"]["name"],
                    )
                    break

                for condition in dp["status"]["conditions"]:
                    if (
                        condition["type"] == "Available"
                        and condition["status"] != "True"
                    ):
                        ready = False
                        logger.info(
                            "Deployment %s is not ready yet",
                            dp["metadata"]["name"],
                        )
                        break

                    if (
                        condition["type"] == "Progressing"
                        and condition["status"] != "True"
                    ):
                        ready = False
                        logger.info(
                            "Deployment %s is not ready yet",
                            dp["metadata"]["name"],
                        )
                        break

            for ds in daemonsets:
                if (
                    ds["status"]["number_ready"]
                    != ds["status"]["current_number_scheduled"]
                ):
                    ready = False
                    logger.info(
                        "Daemonset %s is not ready yet",
                        ds["metadata"]["name"],
                    )
                    break
                if (
                    ds["status"]["desired_number_scheduled"]
                    != ds["status"]["number_ready"]
                ):
                    ready = False
                    logger.info(
                        "Daemonset %s is not ready yet",
                        ds["metadata"]["name"],
                    )
                    break
                if (
                    "updated_number_scheduled" in ds["status"]
                    and ds["status"]["updated_number_scheduled"]
                    != ds["status"]["desired_number_scheduled"]
                ):
                    ready = False
                    logger.info(
                        "Daemonset %s is not ready yet",
                        ds["metadata"]["name"],
                    )
                    break

            if ready:
                # only stop waiting if all deployments and statefulsets are ready
                # else, keep waiting until ready or hard timeout
                break

    event_stream.close()
    timer_hard_timeout.cancel()
    watch_process.terminate()

    time_elapsed = time.strftime(
        "%H:%M:%S", time.gmtime(time.time() - start_timestamp)
    )
    if converge:
        logger.info("System took %s to converge", time_elapsed)
        return True
    else:
        logger.error(
            "System failed to converge within %d seconds", hard_timeout
        )
        return False


def watch_system_events(event_stream, q: multiprocessing.Queue):
    """A process that watches namespaced events"""
    for _ in event_stream:
        try:
            q.put("event")
        except (ValueError, AssertionError):
            pass


class ChactosTrialWorker:
    """
    The distributed wrapper for running in multi processes
    """

    def __init__(
        self,
        worker_id: int,
        work_dir: str,
        workqueue: queue.Queue,
        kubernetes_provider: kind.Kind,
        image_archive: str,
        deployer: Deploy,
        context: dict,
        operator_selector: dict,
        pod_selector: dict,
        priority_pod_selector: Optional[dict] = None,
        diff_exclude_paths: Optional[list[str]] = None,
    ):
        self._worker_id = worker_id
        self._workqueue = workqueue
        self._work_dir = work_dir
        self._kubernetes_provider = kubernetes_provider
        self._images_archive = image_archive
        self._deployer = deployer
        self._context = context
        self._diff_exclude_paths = diff_exclude_paths
        self._operator_selector = operator_selector
        self._priority_pod_selector = priority_pod_selector
        self._pod_selector = pod_selector

    def fault_injection_trial_dir(self, trial_name: str, sequence: int):
        """Return the fault injection trial directory"""
        return os.path.join(
            self._work_dir, f"{trial_name}-fi-worker-{sequence:02d}"
        )

    def __run_steps_until_error(
        self,
        steps: list,
        trial_name: str,
        fault_injection_sequence: int,
        trial: Trial,
        failure: Tuple[FaultType, float],
    ):
        """Run steps until an error occurs or all steps are completed"""
        logger = thread_logger.get_thread_logger()
        fault_injection_trial_dir = self.fault_injection_trial_dir(
            trial_name=trial_name, sequence=fault_injection_sequence
        )
        os.makedirs(fault_injection_trial_dir, exist_ok=True)

        # Set up the Kubernetes cluster
        kubernetes_cluster_name = self._kubernetes_provider.cluster_name(
            acto_namespace=0, worker_id=self._worker_id
        )
        kubernetes_context_name = self._kubernetes_provider.get_context_name(
            kubernetes_cluster_name
        )
        kubernetes_config = os.path.join(
            os.path.expanduser("~"), ".kube", kubernetes_context_name
        )
        self._kubernetes_provider.restart_cluster(
            kubernetes_cluster_name, kubernetes_config
        )
        self._kubernetes_provider.load_images(
            self._images_archive, kubernetes_cluster_name
        )
        kubectl_client = KubectlClient(
            kubernetes_config, kubernetes_context_name
        )
        api_client = kubernetes_client(
            kubernetes_config, kubernetes_context_name
        )

        # Set up the operator and dependencies
        deployed = self._deployer.deploy_with_retry(
            kubernetes_config,
            kubernetes_context_name,
            kubectl_client=kubectl_client,
            namespace=self._context["namespace"],
        )
        if not deployed:
            logger.error("Failed to deploy the operator")
            return

        logger.info("Installing chaos mesh")
        chaosmesh_injector = ChaosMeshFaultInjector()
        chaosmesh_injector.install_with_retry(
            kube_config=kubernetes_config,
            kube_context=kubernetes_context_name,
        )

        fi_trial_dir = self.fault_injection_trial_dir(
            trial_name=trial_name, sequence=fault_injection_sequence
        )
        runner = Runner(
            context=self._context,
            trial_dir=fi_trial_dir,
            kubeconfig=kubernetes_config,
            context_name=kubernetes_context_name,
        )
        initial_step_key = steps.pop(0)
        initial_step = trial.steps[initial_step_key]
        fi_generation = 0

        chactos_snapshot, err = runner.run(
            input_cr=initial_step.snapshot.input_cr,
            generation=fi_generation,
        )
        if err is not None:
            logger.debug("Error when applying initial CR: [%s]", err)
        chactos_snapshot.dump(fi_trial_dir)

        runner.wait_for_system_converge()
        fi_generation += 1

        logger.debug("Starting fault injection on the rest steps")
        while steps:
            step_key = steps[0]
            step = trial.steps[step_key]
            failure_type, pod_failure_ratio = failure

            if (
                step.run_result.is_invalid_input()
                or step.run_result.oracle_result.is_error()
            ):
                steps.pop(0)
                continue

            failures_to_clean_up: list[Failure] = []

            match failure_type:
                case FaultType.POD_FAILURE:
                    pod_failure = PodFailure.build_from_api(
                        api_client=api_client,
                        namespace=self._context["namespace"],
                        pod_selector=self._pod_selector,
                        failure_ratio=pod_failure_ratio,
                        priority_pod_selector=self._priority_pod_selector,
                    )
                    failures_to_clean_up.append(pod_failure)
                case FaultType.NETWORK_DELAY:
                    OperatorApplicationPartitionFailure(
                        operator_selector=self._operator_selector,
                        app_selector=self._pod_selector,
                        namespace=self._context["namespace"],
                    )
                case _:
                    logger.error("Unrecognized type of fault!")

            steady_system_state = runner.collect_system_state()

            try:
                logger.debug(
                    "Injecting %s failure before any CR (policy 1)",
                    failure_type,
                )
                pod_failure.apply(kubectl_client)
            except subprocess.TimeoutExpired:
                logger.warning("Timeout in applying failure.")
                logger.warning(
                    "Current steps: [%s]", sorted(trial.steps.keys())
                )

            logger.debug("Waiting for system to converge for the first failure")
            runner.wait_for_system_converge(hard_timeout=180)

            logger.debug("System converged, now lifting failure")
            pod_failure.cleanup(kubectl_client)

            logger.debug("Waiting for cleanup to converge")
            runner.wait_for_system_converge()
            post_fault_system_state = runner.collect_system_state()

            post_fault_oracle_results = OracleResults()
            diff_result = post_diff_test.compare_system_equality(
                steady_system_state,
                post_fault_system_state,
                additional_exclude_paths=self._diff_exclude_paths,
            )

            # FIXME: this new dir is for the case if steady state FI gets
            # overwritten by actual FI if both have oracle
            post_steady_fault_fi_trial_dir = os.path.join(
                fi_trial_dir, "post-steady-fault"
            )
            os.makedirs(post_steady_fault_fi_trial_dir, exist_ok=True)

            if diff_result:
                post_fault_oracle_results.differential = DifferentialOracleResult(
                    message="failed attempt recovering to steady system state - system state diff",
                    diff=diff_result,
                    from_step=StepID(
                        trial=post_steady_fault_fi_trial_dir,
                        generation=fi_generation,
                    ),
                    from_state=steady_system_state,
                    to_step=StepID(
                        trial=post_steady_fault_fi_trial_dir,
                        generation=fi_generation,
                    ),
                    to_state=post_fault_system_state,
                )
            deprecated_system_state = KubernetesSystemState.from_api_client(
                api_client=api_client,
                namespace=self._context["namespace"],
            )

            health = deprecated_system_state.check_health()
            if not health.is_healthy():
                logger.error("System is not healthy %s post fault", health)
                post_fault_oracle_results.health = OracleResult(
                    message=str(health)
                )

            post_fault_run_result = RunResult(
                testcase={
                    "original_trial": trial_name,
                    "original_step": step_key,
                },
                step_id=StepID(
                    trial=post_steady_fault_fi_trial_dir,
                    generation=fi_generation,
                ),
                oracle_result=post_fault_oracle_results,
                cli_status=check_kubectl_cli(chactos_snapshot),
                is_revert=False,
            )
            post_fault_run_result.dump(post_steady_fault_fi_trial_dir)

            logger.debug(
                "Fault injection on steady state completed, now onto normal fault injection"
            )

            try:
                logger.debug("Injecting %s failure", failure_type)
                pod_failure.apply(kubectl_client)
            except subprocess.TimeoutExpired:
                logger.error("Timeout in applying failure.")
                logger.error("Current steps: [%s]", sorted(trial.steps.keys()))

            logger.debug("Applying next CR")
            chactos_snapshot, err = runner.run(
                input_cr=step.snapshot.input_cr,
                generation=fi_generation,
            )
            if err is not None:
                logger.debug("Error when applying CR: [%s]", err)

            logger.debug("Waiting for CR to converge")
            runner.wait_for_system_converge(hard_timeout=180)

            for f in failures_to_clean_up:
                logger.debug("Cleaning up failure %s", f.name())
                f.cleanup(kubectl_client)

            logger.debug("Waiting for cleanup to converge")
            runner.wait_for_system_converge(hard_timeout=180)

            chactos_snapshot.system_state = runner.collect_system_state()
            chactos_snapshot.dump(fi_trial_dir)

            logger.debug("Acquiring oracle.")

            diff_result = post_diff_test.compare_system_equality(
                chactos_snapshot.system_state,
                step.snapshot.system_state,
                additional_exclude_paths=self._diff_exclude_paths,
            )

            oracle_results = OracleResults()
            if diff_result:
                oracle_results.differential = DifferentialOracleResult(
                    message="failed attempt recovering to seed state - system state diff",
                    diff=diff_result,
                    from_step=StepID(
                        trial=trial_name, generation=int(step_key)
                    ),
                    from_state=step.snapshot.system_state,
                    to_step=StepID(
                        trial=fi_trial_dir,
                        generation=fi_generation,
                    ),
                    to_state=chactos_snapshot.system_state,
                )

            # oracle
            deprecated_system_state = KubernetesSystemState.from_api_client(
                api_client=api_client,
                namespace=self._context["namespace"],
            )

            health = deprecated_system_state.check_health()
            if not health.is_healthy():
                logger.error("System is not healthy %s", health)
                oracle_results.health = OracleResult(message=str(health))

            # Dump the RunResult to disk
            run_result = RunResult(
                testcase={
                    "original_trial": trial_name,
                    "original_step": step_key,
                },
                step_id=StepID(
                    trial=trial_name,
                    generation=fi_generation,
                ),
                oracle_result=oracle_results,
                cli_status=check_kubectl_cli(chactos_snapshot),
                is_revert=False,
            )
            run_result.dump(fi_trial_dir)

            if oracle_results.is_error():
                logger.error("Oracle failed")
                break

            fi_generation += 1
            steps.pop(0)

    def __run_trial(
        self, trial_name: str, trial: Trial, failure: Tuple[FaultType, float]
    ):
        """Run a trial with fault injections"""
        fi_sequence = 0

        steps = sorted(trial.steps.keys())
        while steps:
            self.__run_steps_until_error(
                steps=steps,
                trial_name=trial_name,
                fault_injection_sequence=fi_sequence,
                trial=trial,
                failure=failure,
            )

            fi_sequence += 1

    def run(
        self,
    ) -> None:
        """Run a trial, this function can be parallelized

        This function takes a trial from the Acto's normal test run and
        runs it with fault injections.

        With a normal trial with sequence of empty -> m1 -> m2 -> m3 -> m4,
        it may result in multiple fault injection trials:
        empty -> m1 -> m2 -> m3
        empty -> m3 -> m4
        if m2 -> m3 fails in the fault injection trial.
        """
        logger = thread_logger.get_thread_logger()
        while True:
            try:
                job_details = self._workqueue.get(block=True, timeout=5)
                trial_name: str = job_details[0]
                trial: Trial = job_details[1]
                failure: Tuple[FaultType, float] = job_details[2]
                logger.info("Progress [%d]", self._workqueue.qsize())
            except queue.Empty:
                logger.info("Worker %d finished", self._worker_id)
                break

            self.__run_trial(trial_name, trial, failure)
