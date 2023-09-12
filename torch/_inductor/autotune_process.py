import dataclasses
import logging
import queue
import time
import warnings
from multiprocessing.process import BaseProcess
from multiprocessing.queues import Queue
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING, Union

import torch
from torch import multiprocessing
from torch._dynamo.testing import rand_strided

from torch._inductor import ir
from torch._inductor.codecache import PyCodeCache

if TYPE_CHECKING:
    from torch._inductor.select_algorithm import TritonTemplateCaller

from .utils import do_bench
from .virtualized import V

EXIT_HANDLER_REGISTERED = False

log = logging.getLogger(__name__)


# Used to synchronize between parent and child processes
class Ping:
    pass


class Pong:
    pass


@dataclasses.dataclass
class TuningProcess:
    """
    Abstraction for launching a helper process to benchmark kernels. Spawns
    the parent process and uses multiprocessing queues to send benchmark
    requests and return results.
    """

    process: Optional[BaseProcess] = None
    request_queue: Optional["Queue[Any]"] = None
    response_queue: Optional["Queue[Any]"] = None

    @staticmethod
    def process_main(
        request_queue: "Queue[Any]",
        response_queue: "Queue[Any]",
    ) -> None:
        """
        Entry point for the child process.
        """
        log.debug("Entering TuningProcess child main")
        try:
            TuningProcess.workloop(request_queue, response_queue)
        except Exception as ex:
            log.exception("Exception in TuningProcess: %s", ex)

    @staticmethod
    def workloop(
        request_queue: "Queue[Any]",
        response_queue: "Queue[Any]",
    ) -> None:
        """
        Work loop for the benchmarking subprocess.
        """
        while True:
            obj = request_queue.get()

            if obj is None:
                break  # None is a sentinel for the child to terminate
            elif isinstance(obj, Ping):
                response_queue.put(Pong())
            elif isinstance(obj, BenchmarkRequest):
                response_queue.put(obj.benchmark())
            else:
                raise RuntimeError(f"Invalid request type {type(obj)}")

    def valid(self) -> bool:
        """
        True if the sub-process has been initialized.
        """
        return (
            self.process is not None
            and self.request_queue is not None
            and self.response_queue is not None
        )

    def clear(self) -> None:
        """
        Reset to an uninitialized state.
        """
        self.process = self.request_queue = self.response_queue = None

    def initialize(self) -> None:
        """
        Create child process, request/response queues, and do the warm up.
        """
        if self.valid():
            return

        # cuda runtime does not work with "fork", use "spawn" to start processes.
        ctx = multiprocessing.get_context("spawn")
        self.request_queue = ctx.Queue()
        self.response_queue = ctx.Queue()

        self.process = ctx.Process(
            target=self.process_main,
            args=(
                self.request_queue,
                self.response_queue,
            ),
        )
        assert self.process is not None
        self.process.start()

        # register the exit handler for the parent process so it will terminate
        # the child processes
        global EXIT_HANDLER_REGISTERED
        if not EXIT_HANDLER_REGISTERED:
            EXIT_HANDLER_REGISTERED = True
            import atexit

            atexit.register(lambda: self.terminate())

        # wait for the initialization to be done
        self.put(Ping())
        assert isinstance(self.get(), Pong)

    def put(self, obj: Any) -> None:
        """
        Push a work item to the child process.
        """
        # In case of a prior crash, ensure the subprocess is running
        self.initialize()
        assert self.request_queue is not None
        self.request_queue.put(obj)

    def get(self) -> Any:
        """
        Get a response from the child process.
        """
        assert self.process is not None
        assert self.response_queue is not None
        while True:
            try:
                return self.response_queue.get(timeout=1.0)
            except queue.Empty:
                status = self.process.exitcode
                if status is None:
                    # child process is still running
                    continue
                # child process crashed
                self.clear()
                raise

    def terminate(self) -> None:
        """
        Signal the child process to terminate and wait for it to exit.
        """
        if self.valid():
            assert self.process is not None
            assert self.request_queue is not None
            self.request_queue.put(None)
            self.process.join()
            self.clear()


tuning_process = TuningProcess()


LayoutOrBuffer = Union[ir.Layout, ir.Buffer]


@dataclasses.dataclass
class TensorMeta:
    device: torch.device
    dtype: torch.dtype
    sizes: List[int]
    strides: List[int]
    offset: int

    @classmethod
    def from_irnodes(
        cls, irnodes: Union[LayoutOrBuffer, Tuple[LayoutOrBuffer], List[LayoutOrBuffer]]
    ) -> Union["TensorMeta", List["TensorMeta"]]:
        if isinstance(irnodes, (tuple, list)):
            result: List[Any] = [cls.from_irnodes(x) for x in irnodes]
            assert all(isinstance(x, TensorMeta) for x in result)
            return result

        node = irnodes
        if isinstance(node, ir.Layout):
            node = ir.Buffer("fake", node)

        dtype = node.get_dtype()
        assert dtype is not None

        return TensorMeta(
            device=node.get_device(),
            dtype=dtype,
            sizes=V.graph.sizevars.size_hints(node.get_size()),
            strides=V.graph.sizevars.size_hints(node.get_stride()),
            offset=V.graph.sizevars.size_hint(node.get_layout().offset),
        )

    def to_tensor(self) -> torch.Tensor:
        return rand_strided(
            self.sizes,
            self.strides,
            device=self.device,
            dtype=self.dtype,
            extra_size=self.offset,
        )


@dataclasses.dataclass
class BenchmarkRequest:
    """
    Only handle triton template benchmark for now. The extern kernel benchmark
    can be done inside the same process since they usually don't cause crash.
    """

    module_path: str  # the path of the module defining the triton kernel
    module_cache_key: str
    kernel_name: str  # the kernel name defined in the module
    grid: List[int]
    extra_args: Dict[str, Any]
    num_stages: int
    num_warps: int

    input_tensors: Union["TensorMeta", List["TensorMeta"]]
    output_tensor: Union["TensorMeta", List["TensorMeta"]]

    def benchmark(
        self, *input_tensors: torch.Tensor, output_tensor: Optional[torch.Tensor] = None
    ) -> float:
        debug = log.isEnabledFor(logging.DEBUG)
        if debug:
            start_ts = time.time()

        mod = PyCodeCache.load_by_key_path(self.module_cache_key, self.module_path)
        log.debug(
            "benchmark module key: %s, path: %s",
            self.module_cache_key,
            self.module_path,
        )

        run = getattr(mod, self.kernel_name).run

        if debug:
            load_elapse = time.time() - start_ts
            start_ts = time.time()

        # create args and out tensor
        if output_tensor is None:
            assert len(input_tensors) == 0
            if isinstance(self.input_tensors, List):
                input_tensors = tuple(x.to_tensor() for x in self.input_tensors)
            if isinstance(self.input_tensors, TensorMeta):
                input_tensors = tuple(self.input_tensors.to_tensor())
            assert isinstance(self.output_tensor, TensorMeta)
            output_tensor = self.output_tensor.to_tensor()

        if debug:
            create_tensor_elapse = time.time() - start_ts
            start_ts = time.time()

        def worker() -> float:
            return run(
                *input_tensors,
                output_tensor,
                *self.extra_args,
                grid=self.grid,
                num_stages=self.num_stages,
                num_warps=self.num_warps,
            )

        out = do_bench(worker)
        torch.cuda.synchronize()  # shake out any CUDA errors

        if debug:
            bench_elapse = time.time() - start_ts
            log.debug(
                "InChildProcess %s: load %f, create tensor %f, bench %f",
                self.module_cache_key,
                load_elapse,
                create_tensor_elapse,
                bench_elapse,
            )
        return out


def benchmark_in_sub_process(
    choice: "TritonTemplateCaller",
) -> float:
    """
    Do benchmarking in a subprocess and return the perf number (latency).
    """
    assert choice.bmreq is not None
    tuning_process.initialize()
    assert tuning_process.valid()

    tuning_process.put(choice.bmreq)
    try:
        return tuning_process.get()
    except queue.Empty:
        warnings.warn(
            f"Fail to benchmark choice '{choice}'. It will be ignored. "
            "Please debug the root cause in case the choice can bring perf gains."
        )
        # return INF so this choice will be ignored
        return float("inf")
