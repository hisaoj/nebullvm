import os
import uuid
from typing import Tuple, Dict

import onnx
import torch.cuda

from nebullvm.base import ModelParams, DeepLearningFramework
from nebullvm.config import AUTO_TVM_TUNING_OPTION, AUTO_TVM_PARAMS
from nebullvm.inference_learners.tvm import (
    TVM_INFERENCE_LEARNERS,
    ApacheTVMInferenceLearner,
)
from nebullvm.optimizers.base import BaseOptimizer

try:
    import tvm
    from tvm import IRModule
    from tvm.nd import NDArray
    from tvm.autotvm.tuner import XGBTuner
    from tvm import autotvm
    import tvm.relay as relay
except ImportError:
    import warnings

    warnings.warn("Not found any valid tvm installation")
    # TVM objects needed for avoiding errors
    IRModule = object
    NDArray = object


class ApacheTVMOptimizer(BaseOptimizer):
    def optimize(
        self,
        onnx_model: str,
        output_library: DeepLearningFramework,
        model_params: ModelParams,
    ) -> ApacheTVMInferenceLearner:
        target = self._get_target()
        mod, params = self._build_tvm_model()
        tuning_records = self._tune_tvm_model(target, mod, params)
        with autotvm.apply_history_best(tuning_records):
            with tvm.transform.PassContext(opt_level=3, config={}):
                lib = relay.build(mod, target=target, params=params)
        model = TVM_INFERENCE_LEARNERS[
            self.model.parent_library
        ].from_runtime_module(
            network_parameters=self.model.parameters,
            lib=lib,
            target_device=target,
            input_name="input",
        )
        return model

    @staticmethod
    def _build_tvm_model(
        onnx_model_path: str, model_params: ModelParams
    ) -> Tuple[IRModule, Dict[str, NDArray]]:
        shape_dict = {
            f"input_{i}": (
                model_params.batch_size,
                *input_size,
            )
            for i, input_size in enumerate(model_params.input_sizes)
        }
        onnx_model = onnx.load(onnx_model_path)
        mod, params = relay.frontend.from_onnx(onnx_model, shape_dict)
        return mod, params

    @staticmethod
    def _get_target() -> str:
        force_on_cpu = int(os.getenv("TVM_ON_CPU", 0)) > 1
        if not force_on_cpu and torch.cuda.is_available():
            return str(tvm.target.cuda())
        else:
            return "llvm"  # run on CPU

    @staticmethod
    def _tune_tvm_model(
        target: str, mod: IRModule, params: Dict[str, NDArray]
    ) -> str:
        """Tune the model using AutoTVM."""
        # TODO: add support to Ansor
        tuning_records = f"{uuid.uuid4()}_model_records.json"
        # create a TVM runner
        runner = autotvm.LocalRunner(
            number=AUTO_TVM_PARAMS["number"],
            repeat=AUTO_TVM_PARAMS["repeat"],
            timeout=AUTO_TVM_PARAMS["timeout"],
            min_repeat_ms=AUTO_TVM_PARAMS["min_repeat_ms"],
            # TODO modify min_repeat_ms for GPU usage
            enable_cpu_cache_flush=True,
        )
        # begin by extracting the tasks from the onnx model
        tasks = autotvm.task.extract_from_program(
            mod["main"], target=target, params=params
        )

        # Tune the extracted tasks sequentially.
        for i, task in enumerate(tasks):
            tuner_obj = XGBTuner(task, loss_type="rank")
            tuner_obj.tune(
                n_trial=min(
                    AUTO_TVM_TUNING_OPTION["trials"], len(task.config_space)
                ),
                early_stopping=AUTO_TVM_TUNING_OPTION["early_stopping"],
                measure_option=autotvm.measure_option(
                    builder=autotvm.LocalBuilder(build_func="default"),
                    runner=runner,
                ),
                callbacks=[
                    autotvm.callback.log_to_file(tuning_records),
                ],
            )
        return tuning_records
