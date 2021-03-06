"""
Copyright 2018 Lambda Labs. All Rights Reserved.
Licensed under
==========================================================================

Implement TF application use low level API (tf.train)
"""
from __future__ import print_function
import time
import os

import tensorflow as tf

from backend.tensorflow import tf_app


PS_OPS = ["Variable", "VariableV2", "AutoReloadVariable"]


def assign_to_device(device, ps_device="/cpu:0"):
    def _assign(op):
        node_def = op if isinstance(op, tf.NodeDef) else op.node_def
        if node_def.op in PS_OPS:
            return "/" + ps_device
        else:
            return device
    return _assign


def average_gradients(tower_grads):
  average_grads = []

  for grad_and_vars in zip(*tower_grads):
    # Note that each grad_and_vars looks like the following:
    #   ((grad0_gpu0, var0_gpu0), ... , (grad0_gpuN, var0_gpuN))
    grads = []
    for g, _ in grad_and_vars:
      if g is not None:
        # Add 0 dimension to the gradients to represent the tower.
        expanded_g = tf.expand_dims(g, 0)

        # Append on a "tower" dimension which we will average over below.
        grads.append(expanded_g)

    if grads:
      # Average over the "tower" dimension.
      grad = tf.concat(grads, 0)
      grad = tf.reduce_mean(grad, 0)

      # Keep in mind that the Variables are redundant because they are shared
      # across towers. So we will just return the first tower"s pointer to
      # the Variable.
      v = grad_and_vars[0][1]
      grad_and_var = (grad, v)
      average_grads.append(grad_and_var)
  return average_grads


def average_losses(tower_loss):
  return tf.reduce_mean(tower_loss)


def average_accuracies(tower_accuracies):
  return tf.reduce_mean(tower_accuracies)


class TF_App_Simple(tf_app.TF_App):
  def __init__(self, config):
    super(TF_App_Simple, self).__init__(config)

  def train(self):
    """Training interface
    """
    tf.reset_default_graph()
    if not os.path.isdir(self.config["model"]["dir"]):
      tf.logging.info("Creating model directory %s",
                      self.config["model"]["dir"])
      os.makedirs(self.config["model"]["dir"])

    bs_per_gpu = self.config["train"]["batch_size_per_gpu"]
    save_summary_steps = self.config["run_config"]["save_summary_steps"]
    save_checkpoints_steps = \
        self.config["run_config"]["save_checkpoints_steps"]
    num_gpu = self.config["run_config"]["num_gpu"]

    max_steps = (self.config["data"]["train_num_samples"] *
                 self.config["train"]["epochs"] //
                 self.config["train"]["batch_size"])

    # Build training graph
    with tf.device("/cpu:0"):
      global_step = tf.train.get_or_create_global_step()
      learning_rate = self.create_learning_rate_fn(global_step)

      batch = self.inputter.input_fn("train")
      tower_losses = []
      tower_grads = []
      variables_to_restore = []

      for i in range(num_gpu):
        with tf.device(assign_to_device("/gpu:{}".format(i),
                       ps_device="/cpu:0")):
          _x = batch[0][i * bs_per_gpu:(i + 1) * bs_per_gpu]
          _y = batch[1][i * bs_per_gpu:(i + 1) * bs_per_gpu]

          logits, predictions = self.modeler.create_graph_fn("train", _x)

          # Initialize variables from a pre-trained ckpt
          if "restore_ckpt" in self.config["train"]:
            variables_to_restore = {v.name.split(":")[0]: v
                                    for v in tf.get_collection(
                                        tf.GraphKeys.GLOBAL_VARIABLES)}

            if ("skip_restore_var_list" in self.config["train"]):
              variables_to_restore = {
                v: variables_to_restore[v] for
                v in variables_to_restore if not
                any(x in v for
                    x in self.config["train"]["skip_restore_var_list"])}

          # Pin the trainable variables
          train_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES)
          if "trainable_var_list" in self.config["train"]:
            train_vars = [v for v in train_vars
                          if any(x in v.name
                                 for x in
                                 self.config["train"]["trainable_var_list"])]

          # Compute per-gpu loss and gradient
          loss = self.modeler.create_loss_fn(logits, _y)

          optimizer = self.create_optimizer(learning_rate)

          grads = optimizer.compute_gradients(loss, var_list=train_vars)

          tower_losses.append(loss)
          tower_grads.append(grads)

          if i == 0:
            # Compute training accuracy from the first GPU
            training_accuracy = \
              self.modeler.create_eval_metrics_fn(predictions, _y)
            tf.summary.scalar("training_accuracy", training_accuracy)

      # Compute average loss and gradient
      tower_losses = average_losses(tower_losses)
      tower_grads = average_gradients(tower_grads)

      # # Create train_op to minize the loss
      minimize_op = optimizer.apply_gradients(tower_grads,
                                              global_step=global_step)

      # # Force moving statistics to be updated during training
      update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
      train_op = tf.group(minimize_op, update_ops)

      # Create summary writer
      tf.summary.scalar("train_loss", tower_losses)
      summary_writer = tf.summary.FileWriter(self.config["model"]["dir"],
                                             graph=tf.get_default_graph())
      merged_summary_op = tf.summary.merge_all()

      if variables_to_restore:
        saver_pre_trained = tf.train.Saver(
          var_list=variables_to_restore)

      saver = tf.train.Saver(
        max_to_keep=self.config["run_config"]["keep_checkpoint_max"])

    # Run training
    with tf.Session(config=self.session_config) as sess:
      if variables_to_restore and not tf.train.checkpoint_exists(
        self.config["model"]["dir"] + "/*ckpt*"):

        start = time.time()
        if tf.train.checkpoint_exists(self.config["train"]["restore_ckpt"] +
                                      "/*ckpt*"):
          saver_pre_trained.restore(sess,
                                    tf.train.latest_checkpoint(
                                      self.config["train"]["restore_ckpt"]))
        else:
          raise ValueError("Cannot find pre-trained model to restore from")
        global_vars = tf.global_variables()
        is_not_initialized = sess.run([tf.is_variable_initialized(var)
                                       for var in global_vars])
        not_initialized_vars = [v for (v, f) in
                                zip(global_vars, is_not_initialized) if not f]
        init_op = tf.initialize_variables(not_initialized_vars)
        sess.run(init_op)
        end = time.time()
        print("Restored parameters from " +
              self.config["train"]["restore_ckpt"] +
              " in " + str(end - start) + "sec.")
      elif tf.train.checkpoint_exists(
        self.config["model"]["dir"] + "/*ckpt*"):
        saver.restore(sess,
                      tf.train.latest_checkpoint(
                        self.config["model"]["dir"]))
      else:
        print("Initialize globla variables ... ")
        sess.run(tf.global_variables_initializer())

      _global_step = sess.run(global_step)

      if _global_step >= max_steps:
        print("Training has already reached the maximum steps.")
      else:
        if _global_step == 0:
          print("Start training from step " + str(_global_step))
        else:
          print("Resume training from step " + str(_global_step))

        while _global_step < max_steps:
          _, _loss, _summary, _global_step, _training_accuracy = sess.run(
            [train_op, tower_losses,
             merged_summary_op, global_step, training_accuracy])

          if _global_step % self.config["run_config"]["log_every_n_iter"] == 0:
            print("Step " + str(_global_step) +
                  ": training accuracy " + str(_training_accuracy))

          if _global_step % save_summary_steps == 0:
            summary_writer.add_summary(_summary, _global_step)

          if _global_step % save_checkpoints_steps == 0:
            save_path = saver.save(
              sess,
              self.config["model"]["dir"] + "/model.ckpt",
              global_step=_global_step)
            print("Saving checkpoint " + save_path)

        if max_steps % save_checkpoints_steps != 0:
          print("Saving checkpoint for the last step ...")
          save_path = saver.save(sess,
                                 self.config["model"]["dir"] + "/model.ckpt",
                                 global_step=_global_step)
          print("Checkpoint " + save_path + " has been saved.")
        if max_steps % save_summary_steps != 0:
            summary_writer.add_summary(_summary, _global_step)

      summary_writer.flush()
      summary_writer.close()

  def eval(self):
    """Evaluation interface
    """
    tf.reset_default_graph()
    eval_dir = self.config["model"]["dir"] + "/eval"
    if not os.path.isdir(eval_dir):
      tf.logging.info("Creating model directory %s",
                      eval_dir)
      os.makedirs(eval_dir)

    # Comput miscellaneous
    bs_per_gpu = self.config["eval"]["batch_size_per_gpu"]
    num_gpu = self.config["run_config"]["num_gpu"]
    max_steps = (self.config["data"]["eval_num_samples"] *
                 self.config["eval"]["epochs"] //
                 self.config["eval"]["batch_size"])

    num_log = 10
    steps_per_log = max_steps // num_log

    # Build evaluation graph
    with tf.device("/cpu:0"):
      global_step = tf.train.get_or_create_global_step()

      batch = self.inputter.input_fn("eval")
      tower_accuracies = []

      for i in range(num_gpu):
        with tf.device(assign_to_device("/gpu:{}".format(i),
                       ps_device="/cpu:0")):
          _x = batch[0][i * bs_per_gpu:(i + 1) * bs_per_gpu]
          _y = batch[1][i * bs_per_gpu:(i + 1) * bs_per_gpu]

          logits, predictions = self.modeler.create_graph_fn("eval", _x)

          eval_accuracy = self.modeler.create_eval_metrics_fn(predictions, _y)
          tower_accuracies.append(eval_accuracy)

      accuracy = average_accuracies(tower_accuracies)

    saver = tf.train.Saver(
      max_to_keep=self.config["run_config"]["keep_checkpoint_max"])

    # Run evaluation
    with tf.Session(config=self.session_config) as sess:
      sess.run(tf.global_variables_initializer())

      if tf.train.checkpoint_exists(
        self.config["model"]["dir"] + "/*ckpt*"):
        saver.restore(sess,
                      tf.train.latest_checkpoint(
                        self.config["model"]["dir"]))
      else:
        raise ValueError("Can not find any checkpoint.")

      trained_step = sess.run(global_step)

      accumulated_acc = 0.0
      for step in range(max_steps):
        _accuracy = \
         sess.run(accuracy)
        accumulated_acc = accumulated_acc + _accuracy
        if step % steps_per_log == 0:
          print("Evaluated " + str(step) + " of " + str(max_steps) +
                " steps, running accuracy: " + str(accumulated_acc /
                                                   (step + 1.0)))

      mean_accuracy = accumulated_acc / max_steps
      summary_writer = tf.summary.FileWriter(eval_dir,
                                             graph=tf.get_default_graph())
      summary = tf.Summary()
      summary.value.add(tag="Eval accuracy", simple_value=mean_accuracy)
      summary_writer.add_summary(summary, trained_step)
      summary_writer.flush()
      summary_writer.close()

      print("Evaluation accuracy: " + str(mean_accuracy))

  def infer(self, test_samples):
    """Inference interface
    """
    tf.reset_default_graph()

    # Comput miscellaneous
    bs_per_gpu = self.config["infer"]["batch_size_per_gpu"]
    num_gpu = self.config["run_config"]["num_gpu"]
    max_steps = (len(test_samples) //
                 self.config["infer"]["batch_size"])

    print(test_samples)
    print(max_steps)
    # Build evaluation graph
    with tf.device("/cpu:0"):
      global_step = tf.train.get_or_create_global_step()

      batch = self.inputter.input_fn("infer", test_samples)

      tower_predictions = []

      for i in range(num_gpu):
        with tf.device(assign_to_device("/gpu:{}".format(i),
                       ps_device="/cpu:0")):
          _x = batch[0][i * bs_per_gpu:(i + 1) * bs_per_gpu]
          _y = batch[1][i * bs_per_gpu:(i + 1) * bs_per_gpu]

          logits, predictions = self.modeler.create_graph_fn("infer", _x)

          tower_predictions.append(predictions)

    saver = tf.train.Saver(
      max_to_keep=self.config["run_config"]["keep_checkpoint_max"])

    # Run evaluation
    with tf.Session(config=self.session_config) as sess:
      sess.run(tf.global_variables_initializer())

      if tf.train.checkpoint_exists(
        self.config["model"]["dir"] + "/*ckpt*"):
        saver.restore(sess,
                      tf.train.latest_checkpoint(
                        self.config["model"]["dir"]))
      else:
        raise ValueError("Can not find any checkpoint.")

      for step in range(max_steps):
        _tower_predictions = sess.run(tower_predictions)
        self.modeler.display_prediction_simple(_tower_predictions, test_samples)

  def inspect(self):
    """Inspect interface
    """
    bs_per_gpu = self.config["train"]["batch_size_per_gpu"]
    num_gpu = self.config["run_config"]["num_gpu"]
    max_steps = 8

    batch = self.inputter.input_fn("train")

    if True:
      with tf.Session() as sess:
        sess.run(tf.initialize_all_tables())
        sess.run(tf.global_variables_initializer())
        for i_run in range(max_steps):
          _batch = sess.run(batch)
          print(i_run)
          # print(_batch)
          print(_batch[0].shape)
          print(_batch[1].shape)

    if False:
      # Build training graph
      with tf.device("/cpu:0"):
        batch = self.inputter.input_fn("train")
        global_step = tf.train.get_or_create_global_step()
        learning_rate = self.create_learning_rate_fn(global_step)

        for i in range(num_gpu):
          with tf.device(assign_to_device("/gpu:{}".format(i),
                         ps_device="/cpu:0")):
            _x = batch[0][i * bs_per_gpu:(i + 1) * bs_per_gpu]
            _y = batch[1][i * bs_per_gpu:(i + 1) * bs_per_gpu]

            logits, predictions = self.modeler.create_graph_fn("train", _x)

            # Compute per-gpu loss and gradient
            loss = self.modeler.create_loss_fn(logits, _y)

            if i == 0:
              # Compute training accuracy from the first GPU
              training_accuracy = \
                self.modeler.create_eval_metrics_fn(predictions, _y)
              tf.summary.scalar("training_accuracy", training_accuracy)

    if False:
      # Build training graph
      with tf.device("/cpu:0"):
        batch = self.inputter.input_fn("train")

        for i in range(num_gpu):
          with tf.device(assign_to_device("/gpu:{}".format(i),
                         ps_device="/cpu:0")):
            _x = batch[0][i * bs_per_gpu:(i + 1) * bs_per_gpu]
            _y = batch[1][i * bs_per_gpu:(i + 1) * bs_per_gpu]

            logits, predictions = self.modeler.create_graph_fn("train", _x)

            l2_var_list = [v for v in tf.trainable_variables()]
            if "skip_l2_var_list" in self.config["train"]:
              l2_var_list = [v for v in l2_var_list
                             if not any(x in v.name for
                                        x in self.config["train"]["skip_l2_var_list"])]
            for v in l2_var_list:
              print(v.name)


def build(config):
  """Returns the constructor of the application
  """
  return TF_App_Simple(config)
