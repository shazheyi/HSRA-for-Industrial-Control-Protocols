# encoding=utf8

import itertools
import os
import pickle
from collections import OrderedDict
import time
import numpy as np
import tensorflow as tf
from data_utils import load_word2vec, input_from_line, BatchManager
from loader import augment_with_pretrained, prepare_dataset
from loader import char_mapping, tag_mapping
from loader import load_sentences, update_tag_scheme
from model import Model
from utils import get_logger, make_path, clean, create_model, save_model
from utils import print_config, save_config, load_config, test_ner
import matplotlib.pyplot as plt
import os
os.environ["CUDA_VISIBLE_DEVICES"]="0"  ##表示使用GPU编号为0的GPU进行计算
root_path=os.getcwd()+os.sep
flags = tf.app.flags
flags.DEFINE_boolean("clean",       False,      "clean train folder")
flags.DEFINE_boolean("train",       False,      "Whether train the model")
# configurations for the model
flags.DEFINE_integer("seg_dim",     20,         "Embedding size for segmentation, 0 if not used")
flags.DEFINE_integer("char_dim",    100,        "Embedding size for characters")
flags.DEFINE_integer("lstm_dim",    100,        "Num of hidden units in LSTM, or num of filters in IDCNN")
flags.DEFINE_string("tag_schema",   "iobes",    "tagging schema iobes or iob")

# configurations for training
flags.DEFINE_float("clip",          5,          "Gradient clip")
flags.DEFINE_float("dropout",       0.5,        "Dropout rate")
flags.DEFINE_float("batch_size",    32,         "batch size")
flags.DEFINE_float("lr",            0.0005,      "Initial learning rate")
flags.DEFINE_string("optimizer",    "adam",     "Optimizer for training adam")
flags.DEFINE_boolean("pre_emb",     False,       "Wither use pre-trained embedding")
flags.DEFINE_boolean("zeros",       False,      "Wither replace digits with zero")
flags.DEFINE_boolean("lower",       True,       "Wither lower case")

flags.DEFINE_integer("max_epoch",   10,        "maximum training epochs")
flags.DEFINE_integer("steps_check", 100,        "steps per checkpoint")
flags.DEFINE_string("ckpt_path",    "ckpt",      "Path to save model")
flags.DEFINE_string("summary_path", "summary",      "Path to store summaries")
flags.DEFINE_string("log_file",     "train.log",    "File for log")
flags.DEFINE_string("map_file",     "maps.pkl",     "file for maps")
flags.DEFINE_string("vocab_file",   "vocab.json",   "File for vocab")
flags.DEFINE_string("config_file",  "config_file",  "File for config")
flags.DEFINE_string("script",       "conlleval",    "evaluation script")
flags.DEFINE_string("result_path",  "result",       "Path for results")
flags.DEFINE_string("emb_file",     "D:/szy/ner/ner/data/vec.txt",  "Path for pre_trained embedding")
flags.DEFINE_string("train_file",   "D:/szy/ner/ner/Powerlink/train.txt",  "Path for train data")
flags.DEFINE_string("dev_file",     "D:/szy/ner/ner/Powerlink/dev.txt",    "Path for dev data")
flags.DEFINE_string("test_file",    "D:/szy/ner/ner/Powerlink/test_rd.txt",   "Path for test data")

flags.DEFINE_string("model_type", "idcnn", "Model type, can be idcnn or bilstm")
#flags.DEFINE_string("model_type", "bilstm", "Model type, can be idcnn or bilstm")

FLAGS = tf.app.flags.FLAGS
assert FLAGS.clip < 5.1, "gradient clip should't be too much"
assert 0 <= FLAGS.dropout < 1, "dropout rate between 0 and 1"
assert FLAGS.lr > 0, "learning rate must larger than zero"
assert FLAGS.optimizer in ["adam", "sgd", "adagrad"]


# config for the model
def config_model(char_to_id, tag_to_id):
    config = OrderedDict()
    config["model_type"] = FLAGS.model_type
    config["num_chars"] = len(char_to_id)
    config["char_dim"] = FLAGS.char_dim
    config["num_tags"] = len(tag_to_id)
    config["seg_dim"] = FLAGS.seg_dim
    config["lstm_dim"] = FLAGS.lstm_dim
    config["batch_size"] = FLAGS.batch_size

    config["emb_file"] = FLAGS.emb_file
    config["clip"] = FLAGS.clip
    config["dropout_keep"] = 1.0 - FLAGS.dropout
    config["optimizer"] = FLAGS.optimizer
    config["lr"] = FLAGS.lr
    config["tag_schema"] = FLAGS.tag_schema
    config["pre_emb"] = FLAGS.pre_emb
    config["zeros"] = FLAGS.zeros
    config["lower"] = FLAGS.lower
    return config


def evaluate(sess, model, name, data, id_to_tag, logger):
    logger.info("evaluate:{}".format(name))
    start_time=time.time()
    ner_results = model.evaluate(sess, data, id_to_tag)
    end_time=time.time()
    print("测试时间为",end_time-start_time)
    eval_lines = test_ner(ner_results, FLAGS.result_path)
    for line in eval_lines:
        logger.info(line)
    f1 = float(eval_lines[1].strip().split()[-1])

    if name == "dev":
        best_test_f1 = model.best_dev_f1.eval()
        if f1 > best_test_f1:
            tf.assign(model.best_dev_f1, f1).eval()
            logger.info("new best dev f1 score:{:>.3f}".format(f1))
        return f1 > best_test_f1
    elif name == "test":
        best_test_f1 = model.best_test_f1.eval()
        if f1 > best_test_f1:
            tf.assign(model.best_test_f1, f1).eval()
            logger.info("new best test f1 score:{:>.3f}".format(f1))
        return f1 > best_test_f1


def train():
    # load data sets
    train_sentences = load_sentences(FLAGS.train_file, FLAGS.lower, FLAGS.zeros)
    dev_sentences = load_sentences(FLAGS.dev_file, FLAGS.lower, FLAGS.zeros)
    test_sentences = load_sentences(FLAGS.test_file, FLAGS.lower, FLAGS.zeros)

    # Use selected tagging scheme (IOB / IOBES)
    update_tag_scheme(train_sentences, FLAGS.tag_schema)
    update_tag_scheme(test_sentences, FLAGS.tag_schema)
    update_tag_scheme(dev_sentences, FLAGS.tag_schema)
    # create dictionary for word
    if FLAGS.pre_emb:
        dico_chars_train = char_mapping(train_sentences, FLAGS.lower)[0]
        dico_chars, char_to_id, id_to_char = augment_with_pretrained(
            dico_chars_train.copy(),
            FLAGS.emb_file,
            list(itertools.chain.from_iterable(
                [[w[0] for w in s] for s in test_sentences])
            )
        )
    else:
        _c, char_to_id, id_to_char = char_mapping(train_sentences, FLAGS.lower)

    # Create a dictionary and a mapping for tags
    _t, tag_to_id, id_to_tag = tag_mapping(train_sentences + dev_sentences + test_sentences)
    # with open('maps.txt','w',encoding='utf8') as f1:
    # f1.writelines(str(char_to_id)+" "+id_to_char+" "+str(tag_to_id)+" "+id_to_tag+'\n')
    with open(FLAGS.map_file, "wb") as f:
        pickle.dump([char_to_id, id_to_char, tag_to_id, id_to_tag], f)
    # # create maps if not exist
    # if not os.path.isfile(FLAGS.map_file):
    #     # create dictionary for word
    #     if FLAGS.pre_emb:
    #         dico_chars_train = char_mapping(train_sentences, FLAGS.lower)[0]
    #         dico_chars, char_to_id, id_to_char = augment_with_pretrained(
    #             dico_chars_train.copy(),
    #             FLAGS.emb_file,
    #             list(itertools.chain.from_iterable(
    #                 [[w[0] for w in s] for s in test_sentences])
    #             )
    #         )
    #     else:
    #         _c, char_to_id, id_to_char = char_mapping(train_sentences, FLAGS.lower)
    #
    #     # Create a dictionary and a mapping for tags
    #     _t, tag_to_id, id_to_tag = tag_mapping(train_sentences+dev_sentences+test_sentences)
    #     #with open('maps.txt','w',encoding='utf8') as f1:
    #         #f1.writelines(str(char_to_id)+" "+id_to_char+" "+str(tag_to_id)+" "+id_to_tag+'\n')
    #     with open(FLAGS.map_file, "wb") as f:
    #         pickle.dump([char_to_id, id_to_char, tag_to_id, id_to_tag], f)
    # else:
    #     with open(FLAGS.map_file, "rb") as f:
    #         char_to_id, id_to_char, tag_to_id, id_to_tag = pickle.load(f)

    # prepare data, get a collection of list containing index
    train_data = prepare_dataset(
        train_sentences, char_to_id, tag_to_id, FLAGS.lower
    )
    dev_data = prepare_dataset(
        dev_sentences, char_to_id, tag_to_id, FLAGS.lower
    )
    test_data = prepare_dataset(
        test_sentences, char_to_id, tag_to_id, FLAGS.lower
    )
    print("%i / %i / %i sentences in train / dev / test." % (
        len(train_data), 0, len(test_data)))

    train_manager = BatchManager(train_data, FLAGS.batch_size)
    dev_manager = BatchManager(dev_data, 100)
    test_manager = BatchManager(test_data, 100)
    # make path for store log and model if not exist
    make_path(FLAGS)
    if os.path.isfile(FLAGS.config_file):
        config = load_config(FLAGS.config_file)
    else:
        config = config_model(char_to_id, tag_to_id)
        save_config(config, FLAGS.config_file)
    make_path(FLAGS)

    log_path = os.path.join("log", FLAGS.log_file)
    logger = get_logger(log_path)
    print_config(config, logger)

    # limit GPU memory
    tf_config = tf.ConfigProto(allow_soft_placement=True)
    tf_config.gpu_options.allow_growth = True
    steps_per_epoch = train_manager.len_data
    with tf.Session(config=tf_config) as sess:
        model = create_model(sess, Model, FLAGS.ckpt_path, load_word2vec, config, id_to_char, logger)
        logger.info("start training")
        start_time = time.time()
        loss = []
        all_loss=[]
        with tf.device("/gpu:0"):
            for i in range(FLAGS.max_epoch):
                for batch in train_manager.iter_batch(shuffle=True):
                    step, batch_loss = model.run_step(sess, True, batch)
                    loss.append(batch_loss)
                    if step % FLAGS.steps_check == 0:
                        iteration = step // steps_per_epoch + 1
                        logger.info("iteration:{} step:{}/{}, "
                                    "NER loss:{:>9.6f}".format(
                            iteration, step%steps_per_epoch, steps_per_epoch, np.mean(loss)))
                        all_loss.append(np.mean(loss))
                        loss = []
                if i%2==0:
                    evaluate(sess, model, "dev", dev_manager, id_to_tag, logger)

            end_time = time.time()
            print("训练时间为：",end_time-start_time)
            save_model(sess, model, FLAGS.ckpt_path, logger)
            evaluate(sess, model, "test", test_manager, id_to_tag, logger)

            #画出loss曲线
            # "g"代表green，表示画出的曲线是绿色，"-"表示画出的曲线是实线，label表示图例的名称
            # x=range(1,FLAGS.max_epoch)
            # plt.plot(x,all_loss)
            # plt.legend()
            #
            # plt.xlabel(u'iters')
            # plt.ylabel(u'loss')
            # plt.show()


def evaluate_line():
    config = load_config(FLAGS.config_file)
    logger = get_logger(FLAGS.log_file)
    # limit GPU memory
    tf_config = tf.ConfigProto()
    tf_config.gpu_options.allow_growth = True
    with open(FLAGS.map_file, "rb") as f:
        char_to_id, id_to_char, tag_to_id, id_to_tag = pickle.load(f)
    with tf.Session(config=tf_config) as sess:
        model = create_model(sess, Model, FLAGS.ckpt_path, load_word2vec, config, id_to_char, logger)

        #测试测试集
        test_sentences = load_sentences(FLAGS.test_file, FLAGS.lower, FLAGS.zeros)
        update_tag_scheme(test_sentences, FLAGS.tag_schema)
        test_data = prepare_dataset(
            test_sentences, char_to_id, tag_to_id, FLAGS.lower
        )
        test_manager = BatchManager(test_data, 100)
        evaluate(sess, model, "test", test_manager, id_to_tag, logger)
        # while True:
        #     # try:
        #     #     line = input("请输入测试句子:")
        #     #     result = model.evaluate_line(sess, input_from_line(line, char_to_id), id_to_tag)
        #     #     print(result)
        #     # except Exception as e:
        #     #     logger.info(e)
        #
        #         line = input("请输入测试句子:")
        #         result = model.evaluate_line(sess, input_from_line(line, char_to_id), id_to_tag)
        #         print(result)


def main(_):

    if FLAGS.train:
        if FLAGS.clean:
            clean(FLAGS)
        train()
    else:
        evaluate_line()


if __name__ == "__main__":
    tf.app.run(main)



