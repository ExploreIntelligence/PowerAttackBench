# 看系统级守护进程里的全部网络 
virsh net-list --all 

# 确认映射（应看到 10.10.0.1/24 和 virbr4）
virsh net-dumpxml nilgiri-attacker | grep -E "bridge|ip address" 
 
# 停掉 5 个 nilgiri 网络并禁用自启 
for n in nilgiri-attacker nilgiri-alpha nilgiri-oscar nilgiri-dmz nilgiri-charlie; do 
    virsh net-destroy $n 2>/dev/null && virsh net-autostart $n -- disable; 
done