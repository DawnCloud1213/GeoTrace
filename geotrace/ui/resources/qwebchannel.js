/**
 * Minimal QWebChannel JavaScript API for Qt WebEngine.
 * Provides the QWebChannel constructor used to connect
 * JavaScript code to Python-side QObject instances.
 *
 * This is a standalone replacement for qrc:///qtwebchannel/qwebchannel.js.
 */
"use strict";

var QWebChannelMessageTypes = {
    signal: 1,
    propertyUpdate: 2,
    init: 3,
    idle: 4,
    debug: 5,
    invokeMethod: 6,
    connectToSignal: 7,
    disconnectFromSignal: 8,
    setProperty: 9,
    response: 10,
};

var QWebChannel = function QWebChannel(transport, initCallback) {
    this.transport = transport;
    this.initCallback = initCallback;
    this.objects = {};
    this.execCallbacks = {};
    this.execId = 0;

    var that = this;
    this.transport.onmessage = function (msg) {
        var data = JSON.parse(msg.data);
        switch (data.type) {
        case QWebChannelMessageTypes.signal:
            that._handleSignal(data);
            break;
        case QWebChannelMessageTypes.propertyUpdate:
            that._handlePropertyUpdate(data);
            break;
        case QWebChannelMessageTypes.init:
            that._handleInit(data);
            break;
        case QWebChannelMessageTypes.response:
            that._handleResponse(data);
            break;
        case QWebChannelMessageTypes.idle:
            that._debug("Idle");
            break;
        default:
            that._debug("Unknown message type: " + data.type);
        }
    };

    this._send({ type: QWebChannelMessageTypes.idle });
};

QWebChannel.prototype._send = function (data) {
    this.transport.send(JSON.stringify(data));
};

QWebChannel.prototype._debug = function (msg) {
    if (this.transport.debug) {
        this.transport.debug("QWebChannel: " + msg);
    }
};

QWebChannel.prototype._handleInit = function (data) {
    var that = this;
    data.objects.forEach(function (objInfo) {
        that.objects[objInfo.name] = _wrapObject(objInfo, that);
    });
    if (this.initCallback) {
        this.initCallback(this);
    }
};

QWebChannel.prototype._handleSignal = function (data) {
    var object = this.objects[data.object];
    if (object && object._signals[data.signal] !== undefined) {
        var signalInfo = object._signals[data.signal];
        var args = data.args || [];
        args.unshift(signalInfo.signalName);
        for (var i = 0; i < signalInfo.callbacks.length; i++) {
            signalInfo.callbacks[i].apply(object._object, args);
        }
    }
};

QWebChannel.prototype._handlePropertyUpdate = function (data) {
    var object = this.objects[data.object];
    if (object) {
        object._properties[data.signal] = data.args[0];
        object._object[data.signal] = data.args[0];
    }
};

QWebChannel.prototype._handleResponse = function (data) {
    var cb = this.execCallbacks[data.id];
    if (cb) {
        cb(data.result);
        delete this.execCallbacks[data.id];
    }
};

QWebChannel.prototype._exec = function (obj, method, args, callback) {
    var id = ++this.execId;
    if (callback) {
        this.execCallbacks[id] = callback;
    }
    this._send({
        type: QWebChannelMessageTypes.invokeMethod,
        object: obj,
        method: method,
        args: args,
        id: id
    });
};

function _wrapObject(objInfo, channel) {
    var object = {};
    object._object = object;
    object._id = objInfo.name;
    object._channel = channel;
    object._properties = {};
    object._signals = {};
    object._methods = {};

    // Wrap signals
    if (objInfo.signals) {
        objInfo.signals.forEach(function (sigInfo) {
            var sigName = sigInfo.name;
            object._signals[sigName] = { signalName: sigName, callbacks: [] };
            object[sigName] = {
                connect: function (callback) {
                    var sig = object._signals[this._sigName];
                    if (sig.callbacks.indexOf(callback) < 0) {
                        sig.callbacks.push(callback);
                    }
                    if (!sig.connected) {
                        sig.connected = true;
                        channel._send({
                            type: QWebChannelMessageTypes.connectToSignal,
                            object: object._id,
                            signal: this._sigName
                        });
                    }
                }.bind({ _sigName: sigName }),
                disconnect: function (callback) {
                    var sig = object._signals[this._sigName];
                    var idx = sig.callbacks.indexOf(callback);
                    if (idx >= 0) {
                        sig.callbacks.splice(idx, 1);
                    }
                }.bind({ _sigName: sigName })
            };
        });
    }

    // Wrap methods (slots)
    if (objInfo.methods) {
        objInfo.methods.forEach(function (methodInfo) {
            object[methodInfo.name] = function () {
                var args = Array.prototype.slice.call(arguments);
                channel._exec(object._id, methodInfo.name, args);
            };
        });
    }

    // Wrap properties
    if (objInfo.properties) {
        objInfo.properties.forEach(function (propInfo) {
            var propName = propInfo.name;
            object._properties[propName] = propInfo.value;
            Object.defineProperty(object, propName, {
                get: function () { return object._properties[this._propName]; }.bind({ _propName: propName }),
                set: function (value) {
                    object._properties[this._propName] = value;
                    channel._send({
                        type: QWebChannelMessageTypes.setProperty,
                        object: object._id,
                        property: this._propName,
                        value: value
                    });
                }.bind({ _propName: propName })
            });
        });
    }

    return object;
}
