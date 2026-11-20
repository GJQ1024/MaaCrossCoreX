import type { FullConfig } from '@nekosu/maa-tools'

const config: FullConfig = {
  cwd: import.meta.dirname,
  maaVersion: 'latest',
  interfacePath: 'assets/interface.json',
  check: {
    override: {
      // MPE 编辑器写入的 $__mpe_config_* 会报 warning；不忽略会导致 CI exit 1
      'mpe-config': 'ignore',
      // 本项目大量节点把同一后继同时写进 next 与 on_error（成功/失败都回到同一节点重试），
      // 属既有约定，不是配置错误；忽略以免整库 72 处误报把 CI 打红
      'duplicate-next': 'ignore'
    }
  }
}

export default config
