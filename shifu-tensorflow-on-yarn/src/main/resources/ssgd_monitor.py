"""Synchronous SGD
"""

# from __future__ import print_function
import os
import tensorflow as tf
import argparse
import time
import sys
import logging
import gzip
from StringIO import StringIO
import random
import numpy as np
from tensorflow.python.platform import gfile
from tensorflow.python.framework import ops
from tensorflow.python.saved_model import builder
from tensorflow.python.saved_model import signature_constants
from tensorflow.python.saved_model import signature_def_utils
from tensorflow.python.saved_model import tag_constants
import json
import socket

REPLICAS_TO_AGGREGATE_RATIO = 1
FEATURE_COUNT = 30
HIDDEN_NODES_COUNT = 20
VALID_TRAINING_DATA_RATIO = 0.3
DELIMITER = '|'
BATCH_SIZE = 10
EPOCH = 10 # TODO: should consider recovery from checkpoint, we need to reduce current global step
WORKING_DIR = "hdfs://horton/user/webai/.yarn_cancer/"

# read from env
cluster_spec = json.loads(os.environ["CLUSTER_SPEC"])
n_pss = len(cluster_spec['ps'])  # the number of parameter servers
n_workers = int(os.environ["WORKER_CNT"])  # the number of worker nodes
job_name = os.environ["JOB_NAME"]
task_index = int(os.environ["TASK_ID"])
socket_server_port = int(os.environ["SOCKET_SERVER_PORT"])  # The port of local java socket server listening
first_worker_data_length = int(os.environ["FIRST_WORKER_DATA_LENGTH"]) # Data length in worker0, we use this to estimate total data length

logging.info("job_name:%s, task_index:%d" % (job_name, task_index))

# This client is used for sync worker training intermediate information with master
socket_client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
socket_client.connect(("127.0.0.1", socket_server_port))


def nn_layer(input_tensor, input_dim, output_dim, act=tf.nn.tanh, act_op_name=None):
    l2_reg = tf.contrib.layers.l2_regularizer(scale=0.1)
    weights = tf.get_variable(name="weight_"+str(act_op_name),
                              shape=[input_dim, output_dim],
                              regularizer=l2_reg,
                              initializer=tf.glorot_uniform_initializer())
    biases = tf.get_variable(name="biases_"+str(act_op_name),
                             shape=[output_dim],
                             regularizer=l2_reg,
                             initializer=tf.glorot_uniform_initializer())

    activations = act(tf.matmul(input_tensor, weights) + biases, name=act_op_name)
    return activations


def model(x, y_, sample_weight):
    logging.info("worker_num:%d" % n_workers)
    logging.info("first_worker_data_length:%d" % first_worker_data_length)

    hidden1 = nn_layer(x, FEATURE_COUNT, HIDDEN_NODES_COUNT, act_op_name="hidden_layer1")
    y = nn_layer(hidden1, HIDDEN_NODES_COUNT, 1, act=tf.nn.sigmoid, act_op_name="shifu_output_0")

    # count the number of updates
    global_step = tf.get_variable('global_step', [],
                                  initializer=tf.constant_initializer(0),
                                  trainable=False,
                                  dtype=tf.int32)

    loss = tf.losses.mean_squared_error(predictions=y, labels=y_, weights=sample_weight)

    # we suppose every worker has same batch_size
    opt = tf.train.SyncReplicasOptimizer(
        tf.train.GradientDescentOptimizer(0.003),
        replicas_to_aggregate=n_workers * (first_worker_data_length / BATCH_SIZE) * REPLICAS_TO_AGGREGATE_RATIO,
        total_num_replicas=n_workers * (first_worker_data_length / BATCH_SIZE),
        name="shifu_sync_replicas")
    train_step = opt.minimize(loss, global_step=global_step)

    return opt, train_step, loss, global_step, y


def main(_):
    logging.getLogger().setLevel(logging.INFO)

    ps_hosts = cluster_spec['ps']
    worker_hosts = cluster_spec['worker']
    cluster = tf.train.ClusterSpec({"ps": ps_hosts, "worker": worker_hosts})

    # allows this node know about all other nodes
    if job_name == 'ps':  # checks if parameter server
        server = tf.train.Server(cluster,
                                 job_name="ps",
                                 task_index=task_index)
        server.join()
    else:  # it must be a worker server
        is_chief = (task_index == 0)  # checks if this is the chief node
        server = tf.train.Server(cluster,
                                 job_name="worker",
                                 task_index=task_index)

        logging.info("Loading data from worker index = %d" % task_index)

        if "TRAINING_DATA_PATH" in os.environ:
            logging.info("This is a normal worker..")
            training_data_path = os.environ["TRAINING_DATA_PATH"]
        else:
            logging.info("This is a backup worker")
            # watching certain file in hdfs which contains its training data

        # import data
        context = load_data(training_data_path)

        # split data into batch
        total_batch = int(len(context["train_data"]) / BATCH_SIZE)
        x_batch = np.array_split(context["train_data"], total_batch)
        y_batch = np.array_split(context["train_target"], total_batch)
        sample_w_batch = np.array_split(context["train_data_sample_weight"], total_batch)

        logging.info("Testing set size: %d" % len(context['valid_data']))
        logging.info("Training set size: %d" % len(context['train_data']))

        valid_x = np.asarray(context["valid_data"])
        valid_y = np.asarray(context["valid_target"])
        valid_sample_w = np.asarray(context["valid_data_sample_weight"])

        # Graph
        worker_device = "/job:%s/task:%d" % (job_name, task_index)
        with tf.device(tf.train.replica_device_setter(#ps_tasks=n_pss,
                                                      cluster=cluster,
                                                      worker_device=worker_device
                                                      )):
            input_placeholder = tf.placeholder(dtype=tf.float32, shape=(None, FEATURE_COUNT),
                                               name="shifu_input_0")
            label_placeholder = tf.placeholder(dtype=tf.int32, shape=(None, 1))
            sample_weight_placeholder = tf.placeholder(dtype=tf.float32, shape=(None, 1))

            opt, train_step, loss, global_step, y = model(input_placeholder,
                                                          label_placeholder,
                                                          sample_weight_placeholder)

            # init ops
            init_tokens_op = opt.get_init_tokens_op()
            # initialize local step
            local_init = opt.local_step_init_op
            if is_chief:
                # initializes token queue
                local_init = opt.chief_init_op

            # checks if global vars are init
            ready_for_local_init = opt.ready_for_local_init_op

            # Initializing the variables
            init_op = tf.initialize_all_variables()
            logging.info("---Variables initialized---")

        # **************************************************************************************
        # Session
        sync_replicas_hook = opt.make_session_run_hook(is_chief)
        stop_hook = tf.train.StopAtStepHook(num_steps=EPOCH)
        chief_hooks = [sync_replicas_hook, stop_hook]
        scaff = tf.train.Scaffold(init_op=init_op,
                                  local_init_op=local_init,
                                  ready_for_local_init_op=ready_for_local_init)
        # Configure
        if "IS_BACKUP" in os.environ:
            config = tf.ConfigProto(log_device_placement=False,
                                    allow_soft_placement=True,
                                    device_filters=['/job:ps', '/job:worker/task:0',
                                                    '/job:worker/task:%d' % task_index])
        else:
            config = tf.ConfigProto(log_device_placement=False,
                                    allow_soft_placement=True)

        # Create a "supervisor", which oversees the training process.
        sess = tf.train.MonitoredTrainingSession(master=server.target,
                                                 is_chief=is_chief,
                                                 config=config,
                                                 scaffold=scaff,
                                                 hooks=chief_hooks,
                                                 stop_grace_period_secs=10,
                                                 checkpoint_dir=WORKING_DIR)

        if is_chief:
            sess.run(init_tokens_op)
            logging.info("chief start waiting 40 sec")
            time.sleep(40)  # grace period to wait on other workers before starting training
            logging.info("chief finish waiting 40 sec")

        # Train until hook stops session
        logging.info('Starting training on worker %d' % task_index)
        while not sess.should_stop():
            try:
                start = time.time()
                for i in range(total_batch):
                    train_feed = {input_placeholder: x_batch[i],
                                  label_placeholder: y_batch[i],
                                  sample_weight_placeholder: sample_w_batch[i]}

                    _, l, gs = sess.run([train_step, loss, global_step], feed_dict=train_feed)
                training_time = time.time() - start

                time.sleep(5)

                valid_loss, gs = sess.run([loss, global_step], feed_dict={input_placeholder: valid_x,
                                                                          label_placeholder: valid_y,
                                                                          sample_weight_placeholder: valid_sample_w}
                                          )
                logging.info('step: ' + str(gs) + 'worker: ' + str(task_index) + "training loss:" + str(l) + "valid loss:" + str(valid_loss))

                # Send intermediate result to master
                message = "worker_index:{},time:{},current_epoch:{},training_loss:{},valid_loss:{}\n".format(
                    str(task_index), str(training_time), str(gs), str(l), str(valid_loss))
                if sys.version_info < (3, 0):
                    socket_client.send(bytes(message))
                else:
                    socket_client.send(bytes(message), 'utf8')

            except RuntimeError as re:
                if 'Run called even after should_stop requested.' == re.args[0]:
                    logging.info('About to execute sync_clean_up_op!')
                else:
                    raise

        logging.info('Done' + str(task_index))

        # We just need to make sure chief worker exit with success status is enough
        if is_chief:
            tf.reset_default_graph()

            # add placeholders for input images (and optional labels)
            x = tf.placeholder(dtype=tf.float32, shape=(None, FEATURE_COUNT),
                               name="shifu_input_0")
            with tf.get_default_graph().as_default():
                hidden1 = nn_layer(x, FEATURE_COUNT, HIDDEN_NODES_COUNT, act_op_name="hidden_layer1")
                prediction = nn_layer(hidden1, HIDDEN_NODES_COUNT, 1, act=tf.nn.sigmoid,
                                      act_op_name="shifu_output_0")

            # restore from last checkpoint
            saver = tf.train.Saver()
            with tf.Session() as sess:
                ckpt = tf.train.get_checkpoint_state(WORKING_DIR)
                logging.info("ckpt: {}".format(ckpt))
                assert ckpt, "Invalid model checkpoint path: {}".format(WORKING_DIR)
                saver.restore(sess, ckpt.model_checkpoint_path)

                print("Exporting saved_model to: {}".format(WORKING_DIR + "final/"))

                # exported signatures defined in code
                simple_save(session=sess, export_dir=(WORKING_DIR + "final/"),
                            inputs={
                                "shifu_input_0": x
                            },
                            outputs={
                                "shifu_output_0": prediction
                            })
                print("Exported saved_model")

            time.sleep(40) # grace period to wait before closing session

        #sess.close()
        logging.info('Session from worker %d closed cleanly' % task_index)
        sys.exit()


def load_data(data_file):
    data_file_list = data_file.split(",")
    logging.info("input data %s" % data_file_list)

    train_data = []
    train_target = []
    valid_data = []
    valid_target = []

    training_data_sample_weight = []
    valid_data_sample_weight = []

    train_pos_cnt = 0
    train_neg_cnt = 0
    valid_pos_cnt = 0
    valid_neg_cnt = 0

    # selected feature column number, it will use all feature by default
    feature_column_nums = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25,
                           26, 27, 28, 29, 30]
    sample_weight_column_num = -1
    target_column_num = 0

    file_count = 1
    line_count = 0

    for currentFile in data_file_list:
        logging.info(
            "Now loading " + currentFile + " Progress: " + str(file_count) + "/" + str(len(data_file_list)) + ".")
        file_count += 1

        with gfile.Open(currentFile, 'rb') as f:
            gf = gzip.GzipFile(fileobj=StringIO(f.read()))
            while True:
                line = gf.readline()
                if len(line) == 0:
                    break

                line_count += 1
                if line_count % 10000 == 0:
                    logging.info("Total loading lines: " + str(line_count))

                columns = line.split(DELIMITER)

                if feature_column_nums is None:
                    feature_column_nums = range(0, len(columns))

                    feature_column_nums.remove(target_column_num)
                    if sample_weight_column_num >= 0:
                        feature_column_nums.remove(sample_weight_column_num)

                if random.random() >= VALID_TRAINING_DATA_RATIO:
                    # Append training data
                    train_target.append([float(columns[target_column_num])])
                    if columns[target_column_num] == "1":
                        train_pos_cnt += 1
                    else:
                        train_neg_cnt += 1
                    single_train_data = []
                    for feature_column_num in feature_column_nums:
                        single_train_data.append(float(columns[feature_column_num].strip('\n')))
                    train_data.append(single_train_data)

                    if sample_weight_column_num >= 0 and sample_weight_column_num < len(columns):
                        weight = float(columns[sample_weight_column_num].strip('\n'))
                        if weight < 0.0:
                            logging.info("Warning: weight is below 0. example:" + line)
                            weight = 1.0
                        training_data_sample_weight.append([weight])
                    else:
                        training_data_sample_weight.append([1.0])
                else:
                    # Append validation data
                    valid_target.append([float(columns[target_column_num])])
                    if columns[target_column_num] == "1":
                        valid_pos_cnt += 1
                    else:
                        valid_neg_cnt += 1
                    single_valid_data = []
                    for feature_column_num in feature_column_nums:
                        single_valid_data.append(float(columns[feature_column_num].strip('\n')))
                    valid_data.append(single_valid_data)

                    if  sample_weight_column_num >= 0 and sample_weight_column_num < len(columns):
                        weight = float(columns[sample_weight_column_num].strip('\n'))
                        if weight < 0.0:
                            logging.info("Warning: weight is below 0. example:" + line)
                            weight = 1.0
                        valid_data_sample_weight.append([weight])
                    else:
                        valid_data_sample_weight.append([1.0])

    logging.info("Total data count: " + str(line_count) + ".")
    logging.info("Train pos count: " + str(train_pos_cnt) + ", neg count: " + str(train_neg_cnt) + ".")
    logging.info("Valid pos count: " + str(valid_pos_cnt) + ", neg count: " + str(valid_neg_cnt) + ".")

    return {"train_data": train_data, "train_target": train_target,
            "valid_data": valid_data, "valid_target": valid_target,
            "train_data_sample_weight": training_data_sample_weight,
            "valid_data_sample_weight": valid_data_sample_weight,
            "feature_count": len(feature_column_nums)}


def simple_save(session, export_dir, inputs, outputs, legacy_init_op=None):
    if tf.gfile.Exists(export_dir):
        tf.gfile.DeleteRecursively(export_dir)
    signature_def_map = {
        signature_constants.DEFAULT_SERVING_SIGNATURE_DEF_KEY:
            signature_def_utils.predict_signature_def(inputs, outputs)
    }
    b = builder.SavedModelBuilder(export_dir)
    b.add_meta_graph_and_variables(
        session,
        tags=[tag_constants.SERVING],
        signature_def_map=signature_def_map,
        assets_collection=ops.get_collection(ops.GraphKeys.ASSET_FILEPATHS),
        legacy_init_op=legacy_init_op,
        clear_devices=True)
    b.save()
    export_generic_config(export_dir=export_dir)


def export_generic_config(export_dir):
    config_json_str = ""
    config_json_str += "{\n"
    config_json_str += "    \"inputnames\": [\n"
    config_json_str += "        \"shifu_input_0\"\n"
    config_json_str += "      ],\n"
    config_json_str += "    \"properties\": {\n"
    config_json_str += "         \"algorithm\": \"tensorflow\",\n"
    config_json_str += "         \"tags\": [\"serve\"],\n"
    config_json_str += "         \"outputnames\": \"shifu_output_0\",\n"
    config_json_str += "         \"normtype\": \"ZSCALE\"\n"
    config_json_str += "      }\n"
    config_json_str += "}"
    f = tf.gfile.GFile(export_dir + "GenericModelConfig.json", mode="w+")
    f.write(config_json_str)


if __name__ == '__main__':
    tf.app.run()
