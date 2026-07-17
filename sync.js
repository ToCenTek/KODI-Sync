
var syncIPs = [];
var syncTimes = [];

// 发送 tick
function sendTick(tick) {
    local.send("/syncTick", tick);
}

// 发现组成员
function discoverMulticastMembers() {
    local.send("/discover");
    local.parameters.setCollapsed(true);
    local.values.getChild("multicastMembers").setCollapsed(false); // 展开 Multicast Members
    local.values.multicastMembers.members.set("");                 // 创建前先清空, 否则不更新
    // local.values.getChild("multicastMembers").getChild("members").set("");  // 创建前先清空, 否则不更新,不同的调用方式
}

// 修改本机监听端口, deamon会向新端口上报
function multicastReply(replyPort){
    local.values.alignment.setCollapsed(true);
    local.values.multicastMembers.setCollapsed(false);
    util.delayThreadMS(1000);    // 延时
    local.send("/multicast/reply", replyPort);
}

// 修改组播地址, 提取当前组播成员, 逐个单播新地址, 注意, 必须先发现一次, 拿到组成员 IP 表
// 组播地址范围 (IPv4, Class D) :
// 范围	                            用途	                            能不能用
// 224.0.0.0 ~ 224.0.0.255	    本地网络控制 (OSPF, mDNS, DHCP 等协议用)    ❌ 别用, 跟协议冲突
// 224.0.1.0 ~ 238.255.255.255	公网全局组播	                         ⚠️ 理论上可以, 但路由器可能不转发
// 233.0.0.0/8	                GLOP 组播 (AS 号映射) 	                 ❌ 公网用
// 239.0.0.0 ~ 239.255.255.255	管理作用域 (私有组播) 	                  ✅ 局域网专用, 就是干这个的
function multicastHost(host){
    var members = local.values.multicastMembers.members.get();
    var multicastPort = local.parameters.oscOutputs.oscOutput.remotePort.get();
    // script.log(multicastPort + "\n" + members);

    if (!members) {
        local.values.getChild("multicastMembers").getChild("members").set(
            "THERE MUST BE MEMBERS TO SWITCH MULTICAST GROUP");
        return;
    }
    var ips = members.trim().split("\n");
    for (var i = 0; i < ips.length; i++){
        var ip = ips[i].trim();
        if (ip === "") continue;
        // script.log("IP: " + ip);
        local.sendTo(ip, multicastPort, "/multicast/host", host);    // 单播到每个成员
    }
    local.values.alignment.setCollapsed(true);
    util.delayThreadMS(1000);    // 延时
    discoverMulticastMembers(); // 重新发现
    local.values.multicastMembers.setCollapsed(false);
}

// 修改组播端口, 需要重启 multiscreen-sync, 暂不实现
// 为了防止被修改, 已在初始化阶段设置为只读
function multicastPort(port){
    local.send("/multicast/port", port);
    local.values.alignment.setCollapsed(true);
    local.values.multicastMembers.setCollapsed(false);
    // util.delayThreadMS(1000);    // 延时
}

// 组成员管理, 向成员单播: /member, join/leave
function membersManager(memberIP, args) {
    // var port = root.modules.kodiSync.parameters.oscOutputs.oscOutput.remotePort.get();
    var port = local.parameters.oscOutputs.oscOutput.remotePort.get();
    var address = "/member";
    local.sendTo(memberIP, port, address, args);
    script.log(memberIP, port, address, args);

    util.delayThreadMS(100);    // 延时
    discoverMulticastMembers();
}
// 让组成员构建播放列表
function buildPlaylist() {
    var newVideoDirectory = local.values.vidoeDirectory.get();
    if (newVideoDirectory != null && newVideoDirectory != "") {
        videos = newVideoDirectory;
    } else {
        videos = videoDirectory;
    }
    local.send("/playlist", videos);
}

// /restart: 重启 kodi, /reboot: 重启系统, /shutdown: 关闭系统
function powerControl(command) {
    local.send(command);
}
// 动态添加组员容器
// 为所有活跃成员创建容器 (如不存在)
function updateMemberContainer() {
    var members = local.values.getChild("multicastMembers").getChild("members").get();
    if (!members) return;
    var ips = members.trim().split("\n");

    // 构建现有 IP 容器字典, 避免用 getChild 查不存在的容器触发警告
    var existingContainers = local.values.getContainers();
    var existingMembers = {};
    for (var i = 0; i < existingContainers.length; i++) {
        var memberName = existingContainers[i].niceName;
        if (memberName && memberName.indexOf(".") >= 0) {
            existingMembers[memberName] = true;
        }
    }

    // 遍历活跃 IP, 缺少容器则创建
    for (var j = 0; j < ips.length; j++) {
        var ip = ips[j].trim();
        if (ip === "") continue;

        if (existingMembers[ip]) continue;

        var memberContainer = local.values.addContainer(ip);
        memberContainer.setCollapsed(true);
        memberContainer.addStringParameter("Status", "当前状态", "-----------------");
        memberContainer.getChild("Status").setAttribute("readOnly", true);
        memberContainer.addStringParameter("File", "当前播放的文件路径", "-----------------");
        memberContainer.getChild("File").setAttribute("readOnly", true);

        var playlistContainer = memberContainer.addContainer("Playlist");
        playlistContainer.addStringParameter("Playlist", "Playlist", "-----------------");
        playlistContainer.getChild("Playlist").setAttribute("multiline", true);
        playlistContainer.getChild("Playlist").setAttribute("readOnly", true);
    }
}

// 删除活跃列表中不存在的成员容器
function cleanupMemberContainers() {
    // util.delayThreadMS(200);
    var membersStr = local.values.getChild("multicastMembers").getChild("members").get();
    if (!membersStr) return;
    var activeIPs = membersStr.trim().split("\n");
    var containers = local.values.getContainers();
    var staleNames = [];

    // 遍历容器, 找出不在 activeIPs 中的 IP 容器
    for (var i = 0; i < containers.length; i++) {
        var container = containers[i];
        var memberName = container.niceName;
        if (!memberName || memberName.indexOf(".") < 0) continue;

        var found = false;
        for (var j = 0; j < activeIPs.length; j++) {
            if (activeIPs[j].trim() === memberName) {
                found = true;
                break;
            }
        }
        if (found) continue;

        staleNames.push(memberName);
    }

    // 先收集再删除, 避免边遍历边删导致集合变更
    for (var i = 0; i < staleNames.length; i++) {
        script.log("Member is Leave: " + staleNames[i]);
        local.values.removeContainer(staleNames[i]);
    }
}

// 对齐准备: 值: 播放索引, 对齐时间点(ms)
function alignmentReady(index, position) {
    local.send("/alignment/ready", index, position);
}

// 对齐播放: 值: 播放索引, 对齐时间点(ms), 延迟时间
function alignmentPlay(index, position, delay) {
    local.send("/alignment/play", index, position, delay);
}

// 暂停播放
function play() {
    local.send("/play");
}

// 暂停当前视频
function pause() {
    local.send("/pause");
}

// 停止, 回到主界面(此操作可能会销毁 playerId, 因此需要重新初始化才能播放, 即 buildPlaylistl)
function stopPlay() {
    local.send("/stop");
}

// 播放/暂停: 对当前正在播放的视频有效
function playPause() {
    local.send("/playpause");
}

// seek to position_ms, delay
function seek(position, delay){
    local.send("/seek", position, delay);
}

// 设置音量
function setVolume(volume) {
    local.send("/volume", volume);
}

// 静音
function setMute(isMute) {
    local.send("/mute", isMute);
}

// 查询当前时间, 为暂停态时的追齐搜集数据
function getProperties() {
    local.send("/GetProperties");
}

// 计算偏差, 找出最慢的, 如果组员大于3个, 找出中间值, 单播值外设备, 要求代理追齐
// /kodi/report/current_time : 128738
    // /kodi/report/current_time : 128662
    // /kodi/report/current_time : 128682
    // 从 Chataigne 拿设备列表
// 三个设备的位置
var p1_ip = "", p1_time = 0;
var p2_ip = "", p2_time = 0;
var p3_ip = "", p3_time = 0;
var syncThreshold = 5;  // ms, 超过此值才纠偏
function sync(originIp, timeMs) {
    timeMs = parseInt("" + timeMs, 10);

    if (p1_ip === "" || p1_ip === originIp) { p1_ip = originIp; p1_time = timeMs; }
    else if (p2_ip === "" || p2_ip === originIp) { p2_ip = originIp; p2_time = timeMs; }
    else { p3_ip = originIp; p3_time = timeMs; }

    script.log("sync: " + originIp + "=" + timeMs + " | p1=" + p1_ip + ":" + p1_time + " p2=" + p2_ip + ":" + p2_time + " p3=" + p3_ip + ":" + p3_time);

    if (p1_ip === "" || p2_ip === "" || p3_ip === "") { script.log("等待第三台..."); return; }

    var maxVal = p1_time;
    var maxIp = p1_ip;
    if (p2_time > maxVal) { maxVal = p2_time; maxIp = p2_ip; }
    if (p3_time > maxVal) { maxVal = p3_time; maxIp = p3_ip; }

    script.log("最快: " + maxIp + "=" + maxVal);

    if (maxVal - p1_time > syncThreshold && p1_ip !== maxIp) { script.log("纠偏: " + p1_ip); local.send("/alignment_ready", 2, maxVal, p1_ip); }
    if (maxVal - p2_time > syncThreshold && p2_ip !== maxIp) { script.log("纠偏: " + p2_ip); local.send("/alignment_ready", 2, maxVal, p2_ip); }
    if (maxVal - p3_time > syncThreshold && p3_ip !== maxIp) { script.log("纠偏: " + p3_ip); local.send("/alignment_ready", 2, maxVal, p3_ip); }

    p1_ip = ""; p1_time = 0;
    p2_ip = ""; p2_time = 0;
    p3_ip = ""; p3_time = 0;
    script.log("--- sync done ---");
}

// CPU 亲和性
function cpuAffinity(cpu0, cpi1, cpu2,cpu3) {
    local.send("/cpuAffinity", cpu0, cpi1, cpu2,cpu3);
}

// 设置循环模式
function setLoop(loopmode){
    local.send("/setLoop", loopmode);
}

// /kodi/error <error_code> "<description>"
// /kodi/status/seek <time_ms>
function oscEvent(address, args, originIp) {
    // script.log(originIp + " -> " + address + " " + args[0] + " " + args[1]);
    // /daemon/discover : 10.0.0.88 20:00:01:06:53:1c 20.5.0
    // /daemon/discover : 10.0.0.39 02:00:00:33:15:01 20.5.0
    // /daemon/discover : 10.0.0.69 02:00:00:2b:0e:01 20.5.0
    if (address === "/daemon/discover"){ // 发现组成员
        script.log("discover from: " + originIp);
        var membersContainer = local.values.getChild("multicastMembers").getChild("members");
        var rawContent = membersContainer.get();
        var ipList = rawContent ? rawContent.trim().split("\n") : [];

        // 检查 originIp 是否已在列表中
        var alreadyExists = false;
        for (var memberIndex = 0; memberIndex < ipList.length; memberIndex++) {
            if (ipList[memberIndex].trim() === originIp) {
                alreadyExists = true;
                break;
            }
        }

        // 没有重复则追加
        if (!alreadyExists) {
            ipList.push(originIp);
            var newContent = ipList.join("\n");
            if (newContent.length > 0) newContent += "\n";
            membersContainer.set(newContent);
        }
        updateMemberContainer();
        cleanupMemberContainers();
    }
    if (address === "/kodi/playlist"){
        // var container = local.values.getChild(originIp.split(".").join(""));
        var key = originIp.split(".").join(""); // 去掉 IP 中的 . 因为 chataigne 内部命名没有 .
        var container = local.values.getChild(key); // 在 Values 中找到与 IP 相同的容器
        util.delayThreadMS(100);    // 延时
        if (!container) return;
        if (typeof args[1] === "string") {
            // 提示信息 ("Please wait...")
            // container.getChild("Playlist").getChild("Playlist").set("");
            container.getChild("Playlist").getChild("Playlist").set(args[1]);
            container.setCollapsed(false);
            container.getChild("Playlist").setCollapsed(false);
        } else {
            // 完整列表: args[0] = count, 之后每 6 字段一项
            var count = args[0];
            var lines = [];
            for (var i = 0; i < count; i++) {
                var base = 1 + i * 6;  // 每项 6 字段
                var idx = args[base];
                var name = args[base + 1];
                var dur = args[base + 2];
                var fps = args[base + 3];
                var second_idr = args[base + 4];
                var last_idr = args[base + 5];
                lines.push(idx + ": " + name + " " + dur + " " + fps + " " + second_idr + " " + last_idr);
            }
            var result = lines.join("\n");
            container.getChild("Playlist").getChild("Playlist").set("");
            container.getChild("Playlist").getChild("Playlist").set(result);
            container.setCollapsed(false);
            container.getChild("Playlist").setCollapsed(false);
        }
    }
    if (address === "/kodi/playlist/state"){
        var key = originIp.split(".").join(""); // 去掉 IP 中的 . 因为 chataigne 内部命名没有 .
        var container = local.values.getChild(key); // 在 Values 中找到与 IP 相同的容器
        if (!container) return;
        if (container){
            container.getChild("Status").set("");
            container.getChild("Status").set(args[0] + "  :  " + args[1] + "  :  " + args[2]);
        }

    }
    // /kodi/alignment/ready  <index> <file> <current_ms> <total_hms>
    // /kodi/alignment/ready 0 "4K_29.97-Chimei-inn-RoastDuck.mp4" 1101 "00:03:41.955"
    if (address === "/kodi/alignment/ready"){
        var file = args[1];
        var current_ms = args[2];
        var total_hms = args[3];
        var key = originIp.split(".").join("");
        var container = local.values.getChild(key);
        if (container) {
            container.getChild("File").set("");
            container.getChild("File").set(file);
            container.getChild("Status").set("");
            container.getChild("Status").set(total_hms + "  |  " + current_ms);
            container.setCollapsed(false);
            container.getChild("Playlist").setCollapsed(true);
        }
    }
    // /kodi/alignment/play <index> <file> <current_ms> <total_hms>
    // /kodi/alignment/play 1 0 "4K_29.97-Chimei-inn-RoastDuck.mp4" 6099 "00:03:41.955"
    if (address === "/kodi/alignment/play"){
        var file = args[1];
        var current_ms = args[2];
        var total_hms = args[3];
        var key = originIp.split(".").join(""); // 去掉 IP 中的 . 因为 chataigne 内部命名没有 .
        var container = local.values.getChild(key); // 在 Values 中找到与 IP 相同的容器
        if (container) {
            container.getChild("File").set("");
            container.getChild("File").set(file);
            container.getChild("Status").set("");
            container.getChild("Status").set(total_hms + "  |  " + current_ms);
            container.setCollapsed(false);
        }
    }
    // /kodi/alignment/seek <index> <file> <current_ms> <total_hms>
    if (address === "/kodi/alignment/seek"){
        var file = args[1];
        var current_ms = args[2];
        var total_hms = args[3];
        var key = originIp.split(".").join("");
        var container = local.values.getChild(key);
        if (container) {
            container.getChild("File").set("");
            container.getChild("File").set(file);
            container.getChild("Status").set("");
            container.getChild("Status").set(total_hms + "  |  " + current_ms);
            container.setCollapsed(false);
        }
    }
    if (address === "/kodi/GetProperties"){
        // sync(originIp, args[0]);    // 呼叫 sync 去计算偏差
        var key = originIp.split(".").join(""); // 去掉 IP 中的 . 因为 chataigne 内部命名没有 .
        var container = local.values.getChild(key); // 在 Values 中找到与 IP 相同的容器
        if (container) {
            container.getChild("Status").set("");
            container.getChild("Status").set(args[1] + "  |  " + args[0]);
            container.setCollapsed(false);
        }
    }
    // /kodi/state 0, 1, 'onPlayBackStopped', '', 0, '00:00:00.000'
    // isPaused, isStopped 指示灯状态, 事件, 当前文件,自发上报而来
    if (address === "/kodi/state"){
        var key = originIp.split(".").join(""); // 去掉 IP 中的 . 因为 chataigne 内部命名没有 .
        var container = local.values.getChild(key); // 在 Values 中找到与 IP 相同的容器
        var isPause = local.values.alignment.getChild("isPaused");
        var isStop = local.values.alignment.getChild("isStopped");
        isPause.set(args[0]);
        isStop.set(args[1]);
        var file = args[3];
        var current_ms = args[4];
        var total_hms = args[5];
        if (container){
            container.getChild("File").set("");
            container.getChild("File").set(file);
            container.getChild("Status").set("");
            container.getChild("Status").set(total_hms + "  |  " + current_ms);
        }
    }
    // /kodi/volume <current_volume>
    if (address === "/kodi/volume"){
        volume = local.values.alignment.getChild("volume").set(args);
    }
    // /kodi/error : -1 ERROR: NO ACTIVE PLAYER!!!
    if (address === "/kodi/error"){
        var key = originIp.split(".").join(""); // 去掉 IP 中的 . 因为 chataigne 内部命名没有 .
        var container = local.values.getChild(key); // 在 Values 中找到与 IP 相同的容器
        if (container){
            // container.getChild("File").set("");
            // container.getChild("File").set(file);
            container.getChild("Status").set("");
            container.getChild("Status").set(args[1]);
        }
    }
    // /kodi/mute 1
    if (address === "/kodi/mute"){

        script.log(args[0] + " : " + args[1]);
        // var old_volume = local.values.alignment.getChild("volume").get();
        if (args[0] == 1) {
            local.values.alignment.getChild("volume").setAttribute("readOnly", false);
            local.values.alignment.getChild("volume").set(args[1]);
        }
        if (args[0] == 0) {
            local.values.alignment.getChild("volume").setAttribute("readOnly", true);
            local.values.alignment.getChild("volume").set(args[1]);
        }
    }
    if (address === "/daemon/CPU"){
        script.log(address + " : " + args[0]);
    }
    if (address === "/daemon/member"){
        local.values.multicastMembers.message.set(args[0]);
    }
    if (address === "/daemon/config"){
        local.values.multicastMembers.message.set(args[0]);
    }
    // /system/power : 0 KODI RESTARTING......
    if (address === "/system/power") {
        var key = originIp.split(".").join(""); // 去掉 IP 中的 . 因为 chataigne 内部命名没有 .
        var container = local.values.getChild(key); // 在 Values 中找到与 IP 相同的容器
        if (container){
            container.getChild("File").set("");
            container.getChild("Playlist").getChild("Playlist").set(args[1]);
            container.getChild("Status").set("");
            // container.getChild("Status").set(args[1]);
        }
    }
}
function moduleParameterChanged(param) {
    // 是参数
    if (param.isParameter()){
        script.log("ParameterChanged: " + param.name + " " + param.get());
        // script.logWarning(local.parameters.getControllables(true, false)); // 看看有哪些参数
        // script.logWarning(local.parameters.getContainers());   // 看看有哪些容器
        // script.logWarning(local.parameters.getChild("OSC Outputs").getItems()); // 看看管理器中有什么
        //
        // if (param.name == "tickFrequency") {    // 频率改变
        //     root.modules.tickForSync.parameters.frequency.set(param.get());
        //     local.scripts.sync.updateRate.set(param.get());
        // }
        if (param.name === "localPort"){multicastReply(param.get());}   // 修改了本机监听端口
        if (param.name === "remoteHost"){multicastHost(param.get());}   // 修改了组播地址
        if (param.name === "remotePort"){multicastPort(param.get());}   // 修改了组播端口
    }
    // 是触发器
    else {
        script.log("ParameterChanged: " + param.name);
    }
}

function moduleValueChanged(value) {
    // 是参数
    if (value.isParameter()){
        // script.log("ValueChanged: " + value.name + " : " + value.get());
        if (value.name === "alignmentTime" || value.name === "index") {
            var index = local.values.alignment.getChild("Index").get();
            var position = local.values.alignment.getChild("alignmentTime").get();
            alignmentReady(index, position);
        }
        if (value.name === "alignmentSeek") {        // seek to position and delay
            var delay = local.values.alignment.getChild("alignmentDelay").get();
            seek(value.get(), delay);
            script.log("seek to time: " + delay);
        }
        if (value.name === "manager") {
            var memberIP = local.values.getChild("multicastMembers").getChild("memberIP").get();
            membersManager(memberIP, value.get());
        }
        if (value.name === "alignmentDelay") {
            var index = local.values.alignment.getChild("Index").get();
            var position = local.values.alignment.getChild("alignmentTime").get();
            alignmentPlay(index, position, value.get());
        }
        if (value.name === "message") {local.parameters.setCollapsed(true);}
        if (value.name === "loop") {setLoop(value.get());}
        if (value.name === "volume"){setVolume(value.get());}
        if (value.name === "isMute"){setMute(value.get());}
    }
    // 是触发器
    else{
        script.log("ValueChanged: " + value.name);
        if (value.name === "discoverMulticastMember") {
            discoverMulticastMembers();
            // root.modules.kodiSync.parameters.oscOutputs.oscOutput.remotePort.set(param.get());
        }
        if (value.name === "buildPlaylist") {   // 播放列表构建
            buildPlaylist();
            local.values.getChild("multicastMembers").setCollapsed(true); // 折叠 Multicast Members
            local.values.getChild("powerControl").setCollapsed(true);
            local.values.getChild("alignment").setCollapsed(false);
        }
        if (value.name === "alignmentReady") {  // 对齐准备
            var index = local.values.alignment.getChild("Index").get();
            var position = local.values.alignment.getChild("alignmentTime").get();
            alignmentReady(index, position);
            local.values.multicastMembers.setCollapsed(true);
            local.values.powerControl.setCollapsed(true);
            local.parameters.setCollapsed(true);
        }
        if (value.name === "alignmentPlay") {  // 对齐播放
            var index = local.values.alignment.getChild("Index").get();
            var position = local.values.alignment.getChild("alignmentTime").get();
            var delay = local.values.alignment.getChild("alignmentDelay").get();
            alignmentPlay(index, position, delay);
            local.values.multicastMembers.setCollapsed(true);
            local.values.powerControl.setCollapsed(true);
            local.parameters.setCollapsed(true);
        }
        if (value.name === "play") {    // 恢复播放
            play();
            local.values.multicastMembers.setCollapsed(true);
            local.values.powerControl.setCollapsed(true);
            local.parameters.setCollapsed(true);
        }
        if (value.name === "pause") {   // 暂停
            pause();
            local.values.multicastMembers.setCollapsed(true);
            local.values.powerControl.setCollapsed(true);
            local.parameters.setCollapsed(true);
        }
        if (value.name === "stop") {    // 停止
            stopPlay();
            local.values.multicastMembers.setCollapsed(true);
            local.values.powerControl.setCollapsed(true);
            local.parameters.setCollapsed(true);
        }
        if (value.name === "playPause") {   // 播放/暂停
            playPause();
            local.values.multicastMembers.setCollapsed(true);
            local.values.powerControl.setCollapsed(true);
            local.parameters.setCollapsed(true);
        }
        if (value.name === "getProperties") {   // 查当前时间 ms
            getProperties();
            local.values.multicastMembers.setCollapsed(true);
            local.values.powerControl.setCollapsed(true);
            local.parameters.setCollapsed(true);
        }
        if (value.name === "restartKODI") {powerControl("/restart");}
        if (value.name === "rebootSystem") {powerControl("/reboot");}
        if (value.name === "shutdownSystem") {powerControl("/shutdown");}
    }
}

// function update(deltaTime) {
//     // script.log("Delta time : " + deltaTime);
//     sendTick(deltaTime);
//     // sendTick(local.scripts.sync.params.tickTarget.get());
//     // root.modules.kodiSync.scripts.sync.params.tickTarget
// }
function init(){
    // local.scripts.sync.updateRate.setAttribute("readOnly", false);
    local.parameters.oscOutputs.oscOutput.remotePort.setAttribute("readOnly", true);
    local.parameters.oscOutputs.oscOutput.remotePort.setAttribute("description", "远程主机的组播/单播监听端口\ndeamon 须重启生效, 所以暂不支持修改\n===============================");
    local.parameters.oscOutputs.oscOutput.remoteHost.setAttribute("description", "组播地址, 要切组, 必须先有组成员\n使用 Discover Multicast Member 发现一次即可\n===============================");

    local.parameters.oscInput.localPort.setAttribute("description", "本机监听端口, 组播组成员的上报端口\n修改会通知到组播组以便成员切换\n===============================");


    // var newMetronomeModule = root.modules.getItemWithName("Tick for Sync");
    // if (newMetronomeModule == null) {
    //     newMetronomeModule = root.modules.addItem("Metronome");
    //     if (newMetronomeModule && newMetronomeModule.name != "Tick for Sync") {
    //         newMetronomeModule.setName("Tick for Sync");
    //     }
    // }

    local.scripts.sync.setCollapsed(true);          // 折叠 Scripts.sync
    // local.parameters.getChild("Pass-through").setCollapsed(true);   // 折叠 Pass-Through
    local.parameters.removeContainer("Pass-through"); // 删除 Pass-Through
    local.parameters.getChild("OSC Outputs").setCollapsed(true);    // 折叠 OSC Outputs
    // local.parameters.removeContainer("OSC Outputs"); // 删除 OSC Outputs
    local.parameters.getChild("OSC Input").setCollapsed(true);
    local.values.getChild("multicastMembers").setCollapsed(true); // 折叠 Multicast Members

    script.setUpdateRate(50);

    // var tickTarget = script.addTargetParameter("Tick Target" , "滴答源: Tick for Sync");
    // tickTarget.setAttribute("readOnly", true);

    // root.modules.kodiSync.parameters.oscInput.setName("Input");
    // root.modules.kodiSync.scripts.sync.params.tickTarget;
    // root.modules.kodiSync.commandTester.autoTrigger.set(true);

    // root.modules.kodiSync.scripts.sync.enableLog.set(false);

    // script.log(root.modules.kodiSync.values.multicastMembers.members.get());

    script.log("=============");
    // util.showMessageBox("1", "message 1", "info", "buttonText");
    // script.delay(2000);
    // util.showMessageBox("2", "question 2", "", "buttonText");



}

// function scriptParameterChanged(param) {
//     script.log("Script Parameter Changed: " + param.name);
//     if (param.name === "tickTarget") {
//         var target = param.get();
//         script.log("Target: " + target);
//         if (target === 1) {
//             local.send("/Synctick", param.get());
//         }
//     }
// }
