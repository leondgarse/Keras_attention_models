import tensorflow as tf
from tensorflow import keras
from keras_cv_attention_models.attention_layers import (
    activation_by_name,
    ChannelAffine,
    conv2d_no_bias,
    depthwise_conv2d_no_bias,
    drop_block,
    layer_norm,
    HeadInitializer,
    add_pre_post_process,
)
from keras_cv_attention_models.download_and_load import reload_model_weights

LAYER_NORM_EPSILON = 1e-6
PRETRAINED_DICT = {
    "convnext_tiny": {
        "imagenet": "1deac703865e190528899d5c489afa37",
        "imagenet21k-ft1k": {224: "b70650cc030ec528802762f58940095d", 384: "d6653ede30e25e0c6240f546675393ad"},
    },
    "convnext_small": {
        "imagenet": "7e75873348d445eb2aab4200a5d49f80",
        "imagenet21k-ft1k": {224: "da7c257650b112c1537f2753166fae49", 384: "37ff23f51f2ec9d9b6de2ea7d732ac5f"},
    },
    "convnext_base": {
        "imagenet": {224: "dddac5dcd13bffc1e05688f529726f8c", 384: "ae8dc9bbca6472dc12de30db95ea1018"},
        "imagenet21k-ft1k": {224: "40f78cec6cd327392a9d24f968f9e76b", 384: "4829ff932a930117525920317083d317"},
    },
    "convnext_large": {
        "imagenet": {224: "32d401c254b623d36c22f232884000ba", 384: "01b4e72ca589c2f0ac15551e06d29818"},
        "imagenet21k-ft1k": {224: "dc211e955875f8ab6de7518253e41a46", 384: "68ef87754d6ca634e32d2326c34ddd0b"},
    },
    "convnext_xlarge": {"imagenet21k-ft1k": {224: "7c7ab46f41ac34655f3e035b873a2163", 384: "636db850c0a73ba10e8ab32e91c38df6"}},
    "convnext_v2_atto": {"imagenet": "e604fa1edfefe6207957feec4f5612db"},
    "convnext_v2_base": {
        "imagenet": "879caa3189ed74ed969f9348b82afe47",
        "imagenet21k-ft1k": {224: "8d15a1e29f28e3fd8f0e6691e872ebee", 384: "b267df29706944ec4bc60b57c9778be0"},
    },
    "convnext_v2_femto": {"imagenet": "46d4e39a2efb4dc0aa543442b9000d89"},
    "convnext_v2_large": {
        "imagenet": "18327817424ada5a1c4ea257079e0694",
        "imagenet21k-ft1k": {224: "4bce3ade2680d7181c782b65df8ed929", 384: "6d01f83513538e1f02640314e044d00e"},
    },
    "convnext_v2_nano": {
        "imagenet": "32911de07188225277a47219dbdb4134",
        "imagenet21k-ft1k": {224: "e1761b343263167eb9f4d6c33c6c892d", 384: "2980c5a37ad16cfbc6c90b8a8bb1c83f"},
    },
    "convnext_v2_pico": {"imagenet": "27ed3ae499e0ca6f6b5e3cf8e041ab92"},
    "convnext_v2_tiny": {
        "imagenet": "4b0a70c87400993385b668853c4e3654",
        "imagenet21k-ft1k": {224: "de1db9ab2d8c565767cf81401ceed6ae", 384: "cc9028f2baa22ac1799ca1219e7b2991"},
    },
}



def global_response_normalize(inputs, name=None):
    nn = tf.norm(inputs, axis=(1, 2), keepdims=True)
    nn = nn / (tf.reduce_mean(nn, axis=-1, keepdims=True) + 1e-6)
    nn = ChannelAffine(use_bias=True, weight_init_value=0, name=name and name + "gamma")(inputs * nn)
    return nn + inputs


def block(inputs, output_channel, layer_scale_init_value=1e-6, use_grn=False, drop_rate=0, activation="gelu", name=""):
    nn = depthwise_conv2d_no_bias(inputs, kernel_size=7, padding="SAME", use_bias=True, name=name)
    nn = layer_norm(nn, epsilon=LAYER_NORM_EPSILON, name=name)
    nn = keras.layers.Dense(4 * output_channel, name=name + "up_dense")(nn)
    nn = activation_by_name(nn, activation, name=name)
    if use_grn:
        nn = global_response_normalize(nn, name=name + "grn_")
    nn = keras.layers.Dense(output_channel, name=name + "down_dense")(nn)
    if layer_scale_init_value > 0:
        nn = ChannelAffine(use_bias=False, weight_init_value=layer_scale_init_value, name=name + "gamma")(nn)
    nn = drop_block(nn, drop_rate=drop_rate, name=name)
    return keras.layers.Add(name=name + "output")([inputs, nn])


def ConvNeXt(
    num_blocks=[3, 3, 9, 3],
    out_channels=[96, 192, 384, 768],
    stem_width=-1,
    layer_scale_init_value=1e-6,  # 1e-6 for v1, 0 for v2
    use_grn=False,  # False for v1, True for v2
    head_init_scale=1.0,
    input_shape=(224, 224, 3),
    num_classes=1000,
    activation="gelu",
    drop_connect_rate=0.1,
    classifier_activation="softmax",
    dropout=0,
    pretrained=None,
    model_name="convnext",
    kwargs=None,
):
    inputs = keras.layers.Input(input_shape)

    """ Stem """
    stem_width = stem_width if stem_width > 0 else out_channels[0]
    nn = conv2d_no_bias(inputs, stem_width, kernel_size=4, strides=4, padding="VALID", use_bias=True, name="stem_")
    nn = layer_norm(nn, epsilon=LAYER_NORM_EPSILON, name="stem_")

    """ Blocks """
    total_blocks = sum(num_blocks)
    global_block_id = 0
    for stack_id, (num_block, out_channel) in enumerate(zip(num_blocks, out_channels)):
        stack_name = "stack{}_".format(stack_id + 1)
        if stack_id > 0:
            nn = layer_norm(nn, epsilon=LAYER_NORM_EPSILON, name=stack_name + "downsample_")
            nn = conv2d_no_bias(nn, out_channel, kernel_size=2, strides=2, use_bias=True, name=stack_name + "downsample_")
        for block_id in range(num_block):
            block_name = stack_name + "block{}_".format(block_id + 1)
            block_drop_rate = drop_connect_rate * global_block_id / total_blocks
            nn = block(nn, out_channel, layer_scale_init_value, use_grn, block_drop_rate, activation, name=block_name)
            global_block_id += 1

    """  Output head """
    if num_classes > 0:
        nn = keras.layers.GlobalAveragePooling2D(name="avg_pool")(nn)
        if dropout > 0:
            nn = keras.layers.Dropout(dropout, name="head_drop")(nn)
        nn = layer_norm(nn, epsilon=LAYER_NORM_EPSILON, name="head_")
        head_init = HeadInitializer(scale=head_init_scale)
        nn = keras.layers.Dense(
            num_classes, dtype="float32", activation=classifier_activation, kernel_initializer=head_init, bias_initializer=head_init, name="predictions"
        )(nn)

    model = keras.models.Model(inputs, nn, name=model_name)
    add_pre_post_process(model, rescale_mode="torch")
    reload_model_weights(model, pretrained_dict=PRETRAINED_DICT, sub_release="convnext", pretrained=pretrained)
    return model


def ConvNeXtTiny(input_shape=(224, 224, 3), num_classes=1000, classifier_activation="softmax", pretrained="imagenet", **kwargs):
    num_blocks = [3, 3, 9, 3]
    out_channels = [96, 192, 384, 768]
    return ConvNeXt(**locals(), model_name="convnext_tiny", **kwargs)


def ConvNeXtSmall(input_shape=(224, 224, 3), num_classes=1000, classifier_activation="softmax", pretrained="imagenet", **kwargs):
    num_blocks = [3, 3, 27, 3]
    out_channels = [96, 192, 384, 768]
    return ConvNeXt(**locals(), model_name="convnext_small", **kwargs)


def ConvNeXtBase(input_shape=(224, 224, 3), num_classes=1000, classifier_activation="softmax", pretrained="imagenet", **kwargs):
    num_blocks = [3, 3, 27, 3]
    out_channels = [128, 256, 512, 1024]
    return ConvNeXt(**locals(), model_name="convnext_base", **kwargs)


def ConvNeXtLarge(input_shape=(224, 224, 3), num_classes=1000, classifier_activation="softmax", pretrained="imagenet", **kwargs):
    num_blocks = [3, 3, 27, 3]
    out_channels = [192, 384, 768, 1536]
    return ConvNeXt(**locals(), model_name="convnext_large", **kwargs)


def ConvNeXtXlarge(input_shape=(224, 224, 3), num_classes=1000, classifier_activation="softmax", pretrained="imagenet21k-ft1k", **kwargs):
    num_blocks = [3, 3, 27, 3]
    out_channels = [256, 512, 1024, 2048]
    return ConvNeXt(**locals(), model_name="convnext_xlarge", **kwargs)


""" ConvNeXtV2 """


def ConvNeXtV2(
    num_blocks=[3, 3, 9, 3],
    out_channels=[96, 192, 384, 768],
    stem_width=-1,
    layer_scale_init_value=0,  # 1e-6 for v1, 0 for v2
    use_grn=True,  # False for v1, True for v2
    head_init_scale=1.0,
    input_shape=(224, 224, 3),
    num_classes=1000,
    activation="gelu",
    drop_connect_rate=0.1,
    classifier_activation="softmax",
    dropout=0,
    pretrained=None,
    model_name="convnext_v2",
    kwargs=None,
):
    return ConvNeXt(**locals())


def ConvNeXtV2Atto(input_shape=(224, 224, 3), num_classes=1000, classifier_activation="softmax", pretrained="imagenet21k-ft1k", **kwargs):
    num_blocks = [2, 2, 6, 2]
    out_channels = [40, 80, 160, 320]
    return ConvNeXtV2(**locals(), model_name="convnext_v2_atto", **kwargs)


def ConvNeXtV2Femto(input_shape=(224, 224, 3), num_classes=1000, classifier_activation="softmax", pretrained="imagenet21k-ft1k", **kwargs):
    num_blocks = [2, 2, 6, 2]
    out_channels = [48, 96, 192, 384]
    return ConvNeXtV2(**locals(), model_name="convnext_v2_femto", **kwargs)


def ConvNeXtV2Pico(input_shape=(224, 224, 3), num_classes=1000, classifier_activation="softmax", pretrained="imagenet21k-ft1k", **kwargs):
    num_blocks = [2, 2, 6, 2]
    out_channels = [64, 128, 256, 512]
    return ConvNeXtV2(**locals(), model_name="convnext_v2_pico", **kwargs)


def ConvNeXtV2Nano(input_shape=(224, 224, 3), num_classes=1000, classifier_activation="softmax", pretrained="imagenet21k-ft1k", **kwargs):
    num_blocks = [2, 2, 8, 2]
    out_channels = [80, 160, 320, 640]
    return ConvNeXtV2(**locals(), model_name="convnext_v2_nano", **kwargs)


def ConvNeXtV2Tiny(input_shape=(224, 224, 3), num_classes=1000, classifier_activation="softmax", pretrained="imagenet21k-ft1k", **kwargs):
    num_blocks = [3, 3, 9, 3]
    out_channels = [96, 192, 384, 768]
    return ConvNeXtV2(**locals(), model_name="convnext_v2_tiny", **kwargs)


def ConvNeXtV2Base(input_shape=(224, 224, 3), num_classes=1000, classifier_activation="softmax", pretrained="imagenet21k-ft1k", **kwargs):
    num_blocks = [3, 3, 27, 3]
    out_channels = [128, 256, 512, 1024]
    return ConvNeXtV2(**locals(), model_name="convnext_v2_base", **kwargs)


def ConvNeXtV2Large(input_shape=(224, 224, 3), num_classes=1000, classifier_activation="softmax", pretrained="imagenet21k-ft1k", **kwargs):
    num_blocks = [3, 3, 27, 3]
    out_channels = [192, 384, 768, 1536]
    return ConvNeXtV2(**locals(), model_name="convnext_v2_large", **kwargs)


def ConvNeXtV2Huge(input_shape=(224, 224, 3), num_classes=1000, classifier_activation="softmax", pretrained="imagenet21k-ft1k", **kwargs):
    num_blocks = [3, 3, 27, 3]
    out_channels = [352, 704, 1408, 2816]
    return ConvNeXtV2(**locals(), model_name="convnext_v2_huge", **kwargs)
