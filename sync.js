
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
    // root.modules.kodiSync.values.multicastMembers.members.set(""); // 创建前先清空
    // local.values.multicastMembers.members.set("");                          // 不同的调用方式
    // local.values.getChild("multicastMembers").getChild("members").set("");  // 不同的调用方式
}

// 修改本机监听端口, deamon会向新端口上报
function multicastReply(replyPort){
    local.values.alignment.setCollapsed(true);
    local.values.multicastMembers.setCollapsed(false);
    util.delayThreadMS(1000);    // 延时
    local.send("/multicast/reply", replyPort);
}

// 修改组播地址, 提取当前组播成员, 逐个单播新地址, 注意, 必须先发现一次、拿到组成员 IP 表
// 组播地址范围（IPv4，Class D）：
// 范围	                            用途	                            能不能用
// 224.0.0.0 ~ 224.0.0.255	    本地网络控制（OSPF、mDNS、DHCP 等协议用）   ❌ 别用，跟协议冲突
// 224.0.1.0 ~ 238.255.255.255	公网全局组播	                         ⚠️ 理论上可以，但路由器可能不转发
// 233.0.0.0/8	                GLOP 组播（AS 号映射）	                 ❌ 公网用
// 239.0.0.0 ~ 239.255.255.255	管理作用域（私有组播）	                  ✅ 局域网专用，就是干这个的
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

// 修改组播端口, daemon 需要重启, 暂不实现
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
    local.send("/build_playlist");
    script.log("Notification Members Building Playlist...");  
}

// 动态添加组员容器
function updateMemberContainer() {
    var members = local.values.getChild("multicastMembers").getChild("members").get();
    // script.logWarning(local.values.getContainers());   // 看看有哪些容器
    var kids = local.values.getContainers();
    for (var i = 0; i < kids.length; i++) {
        var child = kids[i];
        script.log("child: " + child.niceName); // 得到容器的友好名字, 如10.0.0.92
        if (child.name === ip) {
            return;
        }
    }
    if (!members) return;
    
    // 遍历组成员 IP
    var ips = members.split("\n");
    for (var i = 0; i < ips.length; i++) {
        var ip = ips[i].trim();
        if (ip === "") continue;
        script.log("Member: " + ip);

        var membersContainer = local.values.addContainer(ip);
        membersContainer.setCollapsed(true);
        membersContainer.addStringParameter("Status", "当前状态", "-----------------");
        membersContainer.getChild("Status").setAttribute("readOnly", true);
        membersContainer.addStringParameter("File", "当前播放的文件路径", "-----------------");
        membersContainer.getChild("File").setAttribute("readOnly", true);

        membersContainer.addContainer("Playlist");
        membersContainer.getChild("Playlist").addStringParameter("Playlist", "Playlist", "-----------------");
        membersContainer.getChild("Playlist").getChild("Playlist").setAttribute("multiline", true);
        membersContainer.getChild("Playlist").getChild("Playlist").setAttribute("readOnly", true);
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
var syncThreshold = 5;  // ms，超过此值才纠偏
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

// 用于设置值, 暂未使用
function xxx(address, args, originIp){
    var key = originIp.split(".").join(""); // 去掉 IP 中的 . 因为 chataigne 内部命名没有 .
    var container = local.values.getChild(key); // 在 Values 中找到与 IP 相同的容器
    if (container) {
        container.getChild("Status").set("");
        container.getChild("Status").set(args[0] + " ms");
    }
}

function oscEvent(address, args, originIp) {
    script.log(originIp + " -> " + address + " " + args[0] + " " + args[1]);
    if (address === "/daemon/discover"){ // 发现组成员
        var members = local.values.getChild("multicastMembers").getChild("members");
        var currentMembers = members.get();
        if (typeof currentMembers !== 'string') currentMembers = "";  // 脏东西洗白成字符串
        var parts = currentMembers.split("\n");
        var lines = [];
        for (var i = 0; i < parts.length; i++) {
            var line = parts[i];
            if (line.trim() === "") continue;
            if (line.length > 0) lines.push(line);  // 如果不是空行, 推进 lines
        }
        // 检查 originIp 是否已经存在
        var exists = false;
        for (var j = 0; j < lines.length; j++) {
            if (lines[j] === originIp) {
                exists = true;
                break;
            }
        }
        // 没有重复, 添加 originIp 进 lines
        if (!exists) {
            lines.push(originIp);   
            var newIP = lines.join("\n");
            if (newIP.length > 0) newIP += "\n";
            members.set(newIP);
        }
        updateMemberContainer();
    } 
    if (address === "/kodi/playlist"){
        var container = local.values.getChild(originIp.split(".").join(""));
        if (!container) return;
        if (typeof args[0] === "string") {
            // 提示信息（"Please wait..."）
            container.getChild("Playlist").getChild("Playlist").set(args[0]);
            container.setCollapsed(false); 
            container.getChild("Playlist").setCollapsed(false); 
        } else {
            // 完整列表：args[0] = count, 之后每 6 字段一项
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
            container.getChild("Playlist").getChild("Playlist").set(result);
            container.setCollapsed(false); 
            container.getChild("Playlist").setCollapsed(false); 
        }
    }

    if (address === "/kodi/alignment/ready"){
        var file = args[2];
        var position = args[3];
        var status = args[4];
        var key = originIp.split(".").join("");
        var container = local.values.getChild(key);
        var isPaused = local.values.alignment.getChild("isPaused");
        isPaused.set(args[0]);
        if (container) {
            container.getChild("File").set("");
            container.getChild("File").set(file);
            container.getChild("Status").set("");
            container.getChild("Status").set(position + " | " + status);
            container.setCollapsed(false);
            container.getChild("Playlist").setCollapsed(true);
        }
    }
    if (address === "/kodi/alignment/play"){
        var file = args[2];
        // var position = args[2];
        var status = args[3];
        var key = originIp.split(".").join(""); // 去掉 IP 中的 . 因为 chataigne 内部命名没有 .
        var container = local.values.getChild(key);
        var isPaused = local.values.alignment.getChild("isPaused");
        isPaused.set(args[0]);
        if (container) {
            container.getChild("File").set("");
            container.getChild("File").set(file);
            container.getChild("Status").set("");
            container.getChild("Status").set(status);
            container.setCollapsed(false); 
        }
    }
    if (address === "/kodi/heartbeat"){
        var key = originIp.split(".").join(""); // 去掉 IP 中的 . 因为 chataigne 内部命名没有 .
        var container = local.values.getChild(key);
        if (container) {
            container.getChild("Status").set("");
            container.getChild("Status").set(args[0] + "");
            container.setCollapsed(false); 
        }
    }
    // /kodi/report/current_time : 128738
    // /kodi/report/current_time : 128662
    // /kodi/report/current_time : 128682
    if (address === "/kodi/GetProperties"){
        // sync(originIp, args[0]);    // 呼叫 sync 去计算偏差
        var key = originIp.split(".").join("");
        var container = local.values.getChild(key);
        if (container) {
            container.getChild("Status").set("");
            container.getChild("Status").set(args[1] + "  |  " + args[0]);
            container.setCollapsed(false); 
        }
    }
    // /kodi/report/member, join or leave
    if (address === "/kodi/member"){
        var key = originIp.split(".").join(""); // 去掉 IP 中的 . 因为 chataigne 内部命名没有 .
        var container = local.values.getChild(key);
        if (container) {
            container.getChild("Status").set("");
            container.getChild("Status").set(args[0] + " ms");
            container.setCollapsed(false); 
        }
    }
    if (address === "/kodi/playpause"){
        var key = originIp.split(".").join(""); // 去掉 IP 中的 . 
        var container = local.values.getChild(key);
        var isPaused = local.values.alignment.getChild("isPaused");
        isPaused.set(args[0]);
        if (container) {
            container.getChild("Status").set("");
            container.getChild("Status").set(args[1]);
            container.setCollapsed(false); 
        }
    }
    if (address === "/kodi/play"){
        var key = originIp.split(".").join(""); // 去掉 IP 中的 . 
        var container = local.values.getChild(key);
        var isPaused = local.values.alignment.getChild("isPaused");
        isPaused.set(args[0]);
        if (container) {
            container.getChild("Status").set("");
            container.getChild("Status").set(args[1]);
            container.setCollapsed(false); 
        }
    }
    if (address === "/kodi/pause"){
        var key = originIp.split(".").join(""); // 去掉 IP 中的 . 
        var container = local.values.getChild(key);
        var isPaused = local.values.alignment.getChild("isPaused");
        isPaused.set(args[0]);
        if (container) {
            container.getChild("Status").set("");
            container.getChild("Status").set(args[1]);
            container.setCollapsed(false); 
        }
    }
    if (address === "/kodi/isPaused"){
        var key = originIp.split(".").join(""); // 去掉 IP 中的 . 
        var container = local.values.getChild(key);
        var isPause = local.values.alignment.getChild("isPaused");
        isPause.set(args[0]);
        if (container) {
            // container.getChild("Status").set("");
            container.getChild("Status").set(args[0]);
            container.setCollapsed(false); 
        }
    }
    if (address === "/kodi/stop"){
        var key = originIp.split(".").join(""); // 去掉 IP 中的 . 
        var container = local.values.getChild(key);
        var isStop = local.values.alignment.getChild("isStopped");
        isStop.set(args[0]);
        if (container) {
            // container.getChild("Status").set("");
            container.getChild("Status").set(args[1]);
            container.setCollapsed(false); 
        }
    }
    if (address === "/kodi/seekToTime"){
        var key = originIp.split(".").join(""); // 去掉 IP 中的 . 
        var container = local.values.getChild(key);
        var isPaused = local.values.alignment.getChild("isPaused"); // 暂停指示灯
        isPaused.set(args[0]);
        if (container) {
            container.getChild("Status").set("");
            container.getChild("Status").set(args[1] + " " + args[2]);
            container.setCollapsed(false); 
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
    if (address === "/kodi/OnAVStart"){
        var key = originIp.split(".").join(""); // 去掉 IP 中的 . 
        var container = local.values.getChild(key);
        var isStop = local.values.alignment.getChild("isStopped");
        isStop.set(args[0]);
        var isPause = local.values.alignment.getChild("isPaused");
        isPause.set(args[0]);
        if (container) {
            // container.getChild("Status").set("");
            container.getChild("File").set(args[1]);
            container.getChild("Status").set(args[2]);
            container.setCollapsed(false); 
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
        script.log("ValueChanged: " + value.name + " : " + value.get());
        if (value.name === "alignmentTime" || value.name === "index") {
            var index = local.values.alignment.getChild("Index").get();
            var position = local.values.alignment.getChild("alignmentTime").get();
            alignmentReady(index, position);
        }
        if (value.name === "loop") {
            setLoop(value.get());
        }
        if (value.name === "seek") {        // seek to position and delay
            var delay = local.values.alignment.getChild("alignmentDelay").get();
            seek(value.get(), delay);
            script.log("seek to time: " + delay);
        } 
        if (value.name === "manager") {
            var memberIP = local.values.getChild("multicastMembers").getChild("memberIP").get();
            membersManager(memberIP, value.get());
        }
        if (value.name === "message") {
            local.parameters.setCollapsed(true);
        }
        if (value.name === "alignmentDelay") {
            var index = local.values.alignment.getChild("Index").get();
            var position = local.values.alignment.getChild("alignmentTime").get();
            alignmentPlay(index, position, value.get());
        }
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
            local.values.getChild("alignment").setCollapsed(false);
        }
        if (value.name === "alignmentReady") {  // 对齐准备
            var index = local.values.alignment.getChild("Index").get();
            var position = local.values.alignment.getChild("alignmentTime").get();
            alignmentReady(index, position);
            local.values.multicastMembers.setCollapsed(true);
            local.parameters.setCollapsed(true);
        }
        if (value.name === "alignmentPlay") {  // 对齐播放
            var index = local.values.alignment.getChild("Index").get();
            var position = local.values.alignment.getChild("alignmentTime").get();
            var delay = local.values.alignment.getChild("alignmentDelay").get();
            alignmentPlay(index, position, delay);
            local.values.multicastMembers.setCollapsed(true);
            local.parameters.setCollapsed(true);
        }
        if (value.name === "play") {    // 恢复播放
            play(); 
            local.values.multicastMembers.setCollapsed(true);
            local.parameters.setCollapsed(true);
        }
        if (value.name === "pause") {   // 暂停
            pause();
            local.values.multicastMembers.setCollapsed(true);
            local.parameters.setCollapsed(true);
        }
        if (value.name === "stop") {    // 停止
            stopPlay();
            local.values.multicastMembers.setCollapsed(true);
            local.parameters.setCollapsed(true);
        }
        if (value.name === "playPause") {   // 播放/暂停
            playPause();
            local.values.multicastMembers.setCollapsed(true);
            local.parameters.setCollapsed(true);
        }
        if (value.name === "getProperties") {   // 查当前时间 ms 
            getProperties();
            local.values.multicastMembers.setCollapsed(true);
            local.parameters.setCollapsed(true);
        }
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

    local.parameters.oscInput.localPort.setAttribute("description", "本机监听端口、组播组成员的上报端口\n修改会通知到组播组以便成员切换\n===============================");


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