# Copyright 2018 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Class CollectiveAllReduceStrategy implementing DistributionStrategy."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from tensorflow.contrib.distribute.python import cross_tower_ops as cross_tower_ops_lib
from tensorflow.contrib.distribute.python import cross_tower_utils
from tensorflow.contrib.distribute.python import mirrored_strategy
from tensorflow.contrib.distribute.python import values
from tensorflow.python.distribute import multi_worker_util
from tensorflow.python.eager import context
from tensorflow.python.framework import ops
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import collective_ops


# TODO(yuefengz): shard the dataset.
# TODO(yuefengz): support in-graph replication.
# TODO(yuefengz): it only works with a cluster without a chief node, maybe
# support chief node?
class CollectiveAllReduceStrategy(mirrored_strategy.MirroredStrategy):
  """Distribution strategy that uses collective ops for all-reduce.

  It is similar to the MirroredStrategy but it uses collective ops for
  reduction.

  When `cluster_spec` is given by the `configure` method, it turns into the
  mulit-worker version that works on multiple workers with between-graph
  replication.

  Note: `configure` will be called by higher-level APIs if running in
  distributed environment.
  """

  def __init__(self, num_gpus_per_worker=0):
    """Initializes the object.

    Args:
      num_gpus_per_worker: number of local GPUs or GPUs per worker.
    """
    self._num_gpus_per_worker = num_gpus_per_worker
    self._initialize(None, None, None)

  def _initialize(self, cluster_spec, task_type, task_id):
    if cluster_spec:
      if task_type is None or task_id is None:
        raise ValueError("When `cluster_spec` is given, you must also specify "
                         "`task_type` and `task_id`")
      if task_type not in ["chief", "worker"]:
        raise ValueError(
            "Unrecognized task_type: %r, valid task types are: \"chief\", "
            "\"worker\"." % task_type)
      self._cluster_spec = multi_worker_util.normalize_cluster_spec(
          cluster_spec)
      worker_device = "/job:%s/task:%d" % (task_type, task_id)
      num_workers = len(self._cluster_spec.as_dict().get(task_type, []))
      if "chief" in self._cluster_spec.as_dict():
        num_workers += 1
      if not num_workers:
        raise ValueError("`task_type` shoud be in `cluster_spec`.")

      self._is_chief = multi_worker_util.is_chief(cluster_spec, task_type,
                                                  task_id)
    else:
      self._cluster_spec = None
      self._is_chief = True
      worker_device = ""
      num_workers = 1
    self._num_workers = num_workers

    if self._num_gpus_per_worker:
      local_devices = [
          "%s/device:GPU:%d" % (worker_device, i)
          for i in range(self._num_gpus_per_worker)
      ]
    else:
      local_devices = [worker_device]

    self._collective_keys = cross_tower_utils.CollectiveKeys()
    super(CollectiveAllReduceStrategy, self).__init__(
        devices=local_devices,
        cross_tower_ops=cross_tower_ops_lib.CollectiveAllReduce(
            num_workers=num_workers,
            num_gpus_per_worker=self._num_gpus_per_worker,
            collective_keys=self._collective_keys))

    # Add a default device so that ops without specified devices will not end up
    # on other workers.
    if cluster_spec:
      self._default_device = "/job:%s/replica:0/task:%d" % (task_type, task_id)

  def _create_variable(self, next_creator, *args, **kwargs):
    colocate_with = kwargs.pop("colocate_with", None)
    devices = self._get_devices_from(colocate_with)
    group_size = len(devices) * self._num_workers
    group_key = self._collective_keys.get_group_key(self._devices)

    def _real_mirrored_creator(devices, *args, **kwargs):
      """Creates one MirroredVariable on the current worker."""
      index = {}
      collective_instance_key = self._collective_keys.get_instance_key(
          key_id=kwargs["name"])
      if "initial_value" not in kwargs:
        raise ValueError("Initial value must be specified.")
      initial_value = kwargs["initial_value"]
      if callable(initial_value):
        initial_value_fn = initial_value
      else:
        initial_value_fn = lambda: initial_value

      for i, d in enumerate(devices):
        with ops.device(d):
          if i > 0:
            # Give replicas meaningful distinct names:
            var0name = index[devices[0]].name.split(":")[0]
            # We append a / to variable names created on towers with id > 0 to
            # ensure that we ignore the name scope and instead use the given
            # name as the absolute name of the variable.
            kwargs["name"] = "%s/replica_%d/" % (var0name, i)

          # The initial value fn makes sure variables all initialized to
          # same values. The first device of the chief worker will send their
          # variable values to other devices and other workers.
          def _overridden_initial_value_fn(device=d, index=i):  # pylint: disable=g-missing-docstring
            with ops.device(device):
              initial_value = initial_value_fn()
              assert not callable(initial_value)
              initial_value = ops.convert_to_tensor(initial_value)

              if self._is_chief and index == 0:
                bcast_send = collective_ops.broadcast_send(
                    initial_value, initial_value.shape, initial_value.dtype,
                    group_size, group_key, collective_instance_key)
                with ops.control_dependencies([bcast_send]):
                  return array_ops.identity(initial_value)
              else:
                return collective_ops.broadcast_recv(
                    initial_value.shape, initial_value.dtype, group_size,
                    group_key, collective_instance_key)

          kwargs["initial_value"] = _overridden_initial_value_fn

          with context.context().device_policy(context.DEVICE_PLACEMENT_SILENT):
            v = next_creator(*args, **kwargs)

          assert not isinstance(v, values.DistributedVariable)
          index[d] = v
      return index

    # pylint: disable=protected-access
    return mirrored_strategy._create_mirrored_variable(
        devices, _real_mirrored_creator, *args, **kwargs)

  def configure(self,
                session_config=None,
                cluster_spec=None,
                task_type=None,
                task_id=None):
    """Configures the object.

    Args:
      session_config: a @{tf.ConfigProto}
      cluster_spec: a dict, ClusterDef or ClusterSpec object specifying the
        cluster configurations.
      task_type: the current task type, such as "worker".
      task_id: the current task id.

    Raises:
      ValueError: if `task_type` is not in the `cluster_spec`.
    """
    # TODO(yuefengz): we'll need to mutate the session_config to add
    # configurations for collective ops.
    del session_config
    if not self._cluster_spec and cluster_spec:
      self._initialize(cluster_spec, task_type, task_id)

  @property
  def between_graph(self):
    return True

  @property
  def should_init(self):
    return True

  @property
  def should_checkpoint(self):
    return self._is_chief

  @property
  def should_save_summary(self):
    return self._is_chief
