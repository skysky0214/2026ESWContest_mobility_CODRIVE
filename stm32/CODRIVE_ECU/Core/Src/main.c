
/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file    main.c
  * @brief   CODRIVE ECU - UART + Classic CAN VehicleState (500 kbit/s)
  ******************************************************************************
  */
/* USER CODE END Header */

#include "main.h"
#include <stdio.h>

/* Private typedef -----------------------------------------------------------*/
/* USER CODE BEGIN PTD */
typedef struct
{
    float speed_kph;
    uint8_t brake_pressed;
    float steering_deg;
    uint32_t last_update_ms;
} VehicleState;
/* USER CODE END PTD */

/* Private define ------------------------------------------------------------*/
/* USER CODE BEGIN PD */
#define CAN_ID_SPEED       0x100U
#define CAN_ID_BRAKE       0x101U
#define CAN_ID_STEERING    0x102U

#define CAN_TEST_PERIOD_MS 500U
/* USER CODE END PD */

/* Private variables ---------------------------------------------------------*/
FDCAN_HandleTypeDef hfdcan1;
UART_HandleTypeDef huart3;

/* USER CODE BEGIN PV */
static uint8_t rx_byte = 0;

volatile uint8_t last_rx_data = 0;
volatile uint32_t rx_count = 0;
volatile uint32_t uart_error_count = 0;
volatile uint32_t last_uart_error = 0;
volatile uint32_t uart_rearm_fail_count = 0;

volatile VehicleState vehicle_state = {0};

volatile uint32_t can_tx_count = 0;
volatile uint32_t can_rx_count = 0;
volatile uint32_t can_tx_error_count = 0;
volatile uint32_t can_rx_error_count = 0;
volatile uint32_t can_last_rx_id = 0;
volatile uint32_t can_last_hal_error = 0;
volatile uint32_t can_init_stage = 0;
volatile uint32_t can_valid_rx_count = 0;
volatile uint32_t can_invalid_frame_count = 0;
volatile uint32_t can_unknown_id_count = 0;
volatile uint32_t can_fifo_lost_count = 0;
volatile uint32_t can_bus_off_count = 0;
volatile uint32_t can_protocol_error_count = 0;
volatile uint32_t can_bus_off = 0;
volatile uint32_t can_last_protocol_error = 0;
volatile uint32_t can_kernel_clock_hz = 0;
volatile uint32_t can_nominal_bitrate = 0;
volatile uint32_t can_speed_rx_count = 0;
volatile uint32_t can_brake_rx_count = 0;
volatile uint32_t can_steering_rx_count = 0;
volatile uint32_t can_speed_last_ms = 0;
volatile uint32_t can_brake_last_ms = 0;
volatile uint32_t can_steering_last_ms = 0;
volatile uint32_t can_tx_error_counter = 0;
volatile uint32_t can_rx_error_counter = 0;
volatile uint32_t telemetry_tx_count = 0;
volatile uint32_t telemetry_skip_count = 0;
static char telemetry_buffer[320];

static uint32_t can_last_test_ms = 0;
static uint16_t test_speed_raw = 0;
static int16_t test_steering_raw = -300;
/* USER CODE END PV */

/* Private function prototypes -----------------------------------------------*/
void SystemClock_Config(void);
static void MX_GPIO_Init(void);
static void MX_USART3_UART_Init(void);
static void MX_FDCAN1_Init(void);

/* USER CODE BEGIN PFP */
static void CAN_Setup(void);
static void CAN_Send(uint32_t id, const uint8_t data[8]);
static void CAN_ProcessRx(void);
static void CAN_SendTestData(void);
static void CAN_CheckStatus(void);
static void UART_SendVehicleTelemetry(void);
/* USER CODE END PFP */

/* USER CODE BEGIN 0 */

void HAL_UART_RxCpltCallback(UART_HandleTypeDef *huart)
{
    if (huart->Instance != USART3)
    {
        return;
    }

    last_rx_data = rx_byte;
    rx_count++;

    switch (rx_byte)
    {
        case '1':
            HAL_GPIO_WritePin(GPIOB, GPIO_PIN_0, GPIO_PIN_SET);
            break;

        case '0':
            HAL_GPIO_WritePin(GPIOB, GPIO_PIN_0, GPIO_PIN_RESET);
            break;

        case 'T':
        case 't':
            HAL_GPIO_TogglePin(GPIOB, GPIO_PIN_0);
            break;

        default:
            break;
    }

    if (HAL_UART_Receive_IT(&huart3, &rx_byte, 1) != HAL_OK)
    {
        uart_rearm_fail_count++;
    }
}

void HAL_UART_ErrorCallback(UART_HandleTypeDef *huart)
{
    if (huart->Instance != USART3)
    {
        return;
    }

    uart_error_count++;
    last_uart_error = HAL_UART_GetError(huart);

    if (huart->RxState == HAL_UART_STATE_READY)
    {
        if (HAL_UART_Receive_IT(&huart3, &rx_byte, 1) != HAL_OK)
        {
            uart_rearm_fail_count++;
        }
    }
}

static void CAN_Setup(void)
{
    FDCAN_FilterTypeDef filter = {0};

    can_init_stage = 1;

    can_kernel_clock_hz = HAL_RCCEx_GetPeriphCLKFreq(RCC_PERIPHCLK_FDCAN);
    can_nominal_bitrate = can_kernel_clock_hz /
        (hfdcan1.Init.NominalPrescaler *
         (1U + hfdcan1.Init.NominalTimeSeg1 + hfdcan1.Init.NominalTimeSeg2));
    if (can_kernel_clock_hz != 128000000U || can_nominal_bitrate != 500000U)
    {
        Error_Handler();
    }

    filter.IdType = FDCAN_STANDARD_ID;
    filter.FilterIndex = 0;
    filter.FilterType = FDCAN_FILTER_MASK;
    filter.FilterConfig = FDCAN_FILTER_TO_RXFIFO0;
    filter.FilterID1 = 0;
    filter.FilterID2 = 0;

    if (HAL_FDCAN_ConfigFilter(&hfdcan1, &filter) != HAL_OK)
    {
        can_last_hal_error = HAL_FDCAN_GetError(&hfdcan1);
        Error_Handler();
    }

    can_init_stage = 2;

    if (HAL_FDCAN_ConfigGlobalFilter(
            &hfdcan1,
            FDCAN_REJECT,
            FDCAN_REJECT,
            FDCAN_REJECT_REMOTE,
            FDCAN_REJECT_REMOTE) != HAL_OK)
    {
        can_last_hal_error = HAL_FDCAN_GetError(&hfdcan1);
        Error_Handler();
    }

    can_init_stage = 3;

    if (HAL_FDCAN_Start(&hfdcan1) != HAL_OK)
    {
        can_last_hal_error = HAL_FDCAN_GetError(&hfdcan1);
        Error_Handler();
    }

    can_init_stage = 4;
}

static void CAN_Send(uint32_t id, const uint8_t data[8])
{
    FDCAN_TxHeaderTypeDef tx_header = {0};

    tx_header.Identifier = id;
    tx_header.IdType = FDCAN_STANDARD_ID;
    tx_header.TxFrameType = FDCAN_DATA_FRAME;
    tx_header.DataLength = FDCAN_DLC_BYTES_8;
    tx_header.ErrorStateIndicator = FDCAN_ESI_ACTIVE;
    tx_header.BitRateSwitch = FDCAN_BRS_OFF;
    tx_header.FDFormat = FDCAN_CLASSIC_CAN;
    tx_header.TxEventFifoControl = FDCAN_NO_TX_EVENTS;
    tx_header.MessageMarker = 0;

    if (HAL_FDCAN_AddMessageToTxFifoQ(
            &hfdcan1,
            &tx_header,
            (uint8_t *)data) == HAL_OK)
    {
        can_tx_count++;
    }
    else
    {
        can_tx_error_count++;
        can_last_hal_error = HAL_FDCAN_GetError(&hfdcan1);
    }
}

static void CAN_SendTestData(void)
{
    uint8_t speed_data[8] = {0};
    uint8_t brake_data[8] = {0};
    uint8_t steering_data[8] = {0};

    uint16_t steering_bits;

    test_speed_raw += 50U;

    if (test_speed_raw > 10000U)
    {
        test_speed_raw = 0;
    }

    test_steering_raw += 100;

    if (test_steering_raw > 300)
    {
        test_steering_raw = -300;
    }

    /* Speed: uint16 little-endian, 0.01 km/h */
    speed_data[0] = (uint8_t)(test_speed_raw & 0xFFU);
    speed_data[1] = (uint8_t)((test_speed_raw >> 8) & 0xFFU);

    /* Brake: 0 = OFF, 1 = ON */
    brake_data[0] =
        ((test_speed_raw / 50U) % 2U) ? 1U : 0U;

    /* Steering: signed int16 little-endian, 0.1 degree */
    steering_bits = (uint16_t)test_steering_raw;

    steering_data[0] = (uint8_t)(steering_bits & 0xFFU);
    steering_data[1] = (uint8_t)((steering_bits >> 8) & 0xFFU);

    CAN_Send(CAN_ID_SPEED, speed_data);
    CAN_Send(CAN_ID_BRAKE, brake_data);
    CAN_Send(CAN_ID_STEERING, steering_data);
}

static void CAN_ProcessRx(void)
{
    FDCAN_RxHeaderTypeDef rx_header = {0};
    uint8_t rx_data[8] = {0};

    while (HAL_FDCAN_GetRxFifoFillLevel(
               &hfdcan1, FDCAN_RX_FIFO0) > 0U)
    {
        if (HAL_FDCAN_GetRxMessage(
                &hfdcan1,
                FDCAN_RX_FIFO0,
                &rx_header,
                rx_data) != HAL_OK)
        {
            can_rx_error_count++;
            can_last_hal_error = HAL_FDCAN_GetError(&hfdcan1);
            break;
        }

        can_rx_count++;
        can_last_rx_id = rx_header.Identifier;

        if (rx_header.IdType != FDCAN_STANDARD_ID ||
            rx_header.RxFrameType != FDCAN_DATA_FRAME ||
            rx_header.DataLength != FDCAN_DLC_BYTES_8 ||
            rx_header.FDFormat != FDCAN_CLASSIC_CAN)
        {
            can_invalid_frame_count++;
            continue;
        }

        switch (rx_header.Identifier)
        {
            case CAN_ID_SPEED:
            {
                uint16_t raw =
                    (uint16_t)rx_data[0] |
                    ((uint16_t)rx_data[1] << 8);

                vehicle_state.speed_kph = raw * 0.01f;
                vehicle_state.last_update_ms = HAL_GetTick();
                can_speed_last_ms = vehicle_state.last_update_ms;
                can_speed_rx_count++;
                can_valid_rx_count++;
                break;
            }

            case CAN_ID_BRAKE:
            {
                if (rx_data[0] > 1U)
                {
                    can_invalid_frame_count++;
                    break;
                }
                vehicle_state.brake_pressed =
                    (rx_data[0] != 0U) ? 1U : 0U;

                vehicle_state.last_update_ms = HAL_GetTick();
                can_brake_last_ms = vehicle_state.last_update_ms;
                can_brake_rx_count++;
                can_valid_rx_count++;
                break;
            }

            case CAN_ID_STEERING:
            {
                uint16_t bits =
                    (uint16_t)rx_data[0] |
                    ((uint16_t)rx_data[1] << 8);

                int32_t signed_raw =
                    (bits & 0x8000U)
                    ? ((int32_t)bits - 65536)
                    : (int32_t)bits;

                vehicle_state.steering_deg =
                    signed_raw * 0.1f;

                vehicle_state.last_update_ms = HAL_GetTick();
                can_steering_last_ms = vehicle_state.last_update_ms;
                can_steering_rx_count++;
                can_valid_rx_count++;
                break;
            }

            default:
                can_unknown_id_count++;
                break;
        }
    }
}

/* Poll status as well as FIFO; no second receive path or new UART protocol. */
static void CAN_CheckStatus(void)
{
    static uint32_t last_check_ms = 0;
    FDCAN_ProtocolStatusTypeDef status = {0};
    FDCAN_ErrorCountersTypeDef counters = {0};

    if (__HAL_FDCAN_GET_FLAG(&hfdcan1, FDCAN_FLAG_RX_FIFO0_MESSAGE_LOST))
    {
        __HAL_FDCAN_CLEAR_FLAG(&hfdcan1, FDCAN_FLAG_RX_FIFO0_MESSAGE_LOST);
        can_fifo_lost_count++; /* Latched events, not an exact lost-frame count. */
    }
    if ((HAL_GetTick() - last_check_ms) < 100U)
    {
        return;
    }
    last_check_ms = HAL_GetTick();
    if (HAL_FDCAN_GetProtocolStatus(&hfdcan1, &status) == HAL_OK)
    {
        if (status.BusOff && !can_bus_off)
        {
            can_bus_off_count++;
        }
        can_bus_off = status.BusOff;
        if (status.LastErrorCode > 0U && status.LastErrorCode < 7U)
        {
            can_last_protocol_error = status.LastErrorCode;
            can_protocol_error_count++; /* Error observations between status reads. */
        }
    }
    else
    {
        can_rx_error_count++;
        can_last_hal_error = HAL_FDCAN_GetError(&hfdcan1);
    }
    if (HAL_FDCAN_GetErrorCounters(&hfdcan1, &counters) == HAL_OK)
    {
        can_tx_error_counter = counters.TxErrorCnt;
        can_rx_error_counter = counters.RxErrorCnt;
    }
}

/* Nonblocking TX; UART RX interrupt and existing LED commands stay active.
 * Static buffer is only overwritten once the previous TX has completed. */
static void UART_SendVehicleTelemetry(void)
{
    static uint32_t last_ms = 0;
    uint32_t now = HAL_GetTick();
    int length;
    int32_t steering_tenths;
    if ((now - last_ms) < 200U)
    {
        return;
    }
    last_ms = now;
    if (huart3.gState != HAL_UART_STATE_READY)
    {
        telemetry_skip_count++;
        return;
    }
    steering_tenths = (int32_t)(vehicle_state.steering_deg * 10.0f +
                         ((vehicle_state.steering_deg >= 0.0f) ? 0.5f : -0.5f));
    length = snprintf(telemetry_buffer, sizeof(telemetry_buffer),
        "ECU,ms=%lu,speed_raw=%lu,brake=%u,steer_raw=%ld,rx=%lu,valid=%lu,"
        "speed_n=%lu,brake_n=%lu,steer_n=%lu,speed_ms=%lu,brake_ms=%lu,steer_ms=%lu,"
        "err=%lu,invalid=%lu,lost=%lu,bo=%lu,lec=%lu\r\n",
        (unsigned long)now,
        (unsigned long)(vehicle_state.speed_kph * 100.0f + 0.5f),
        (unsigned int)vehicle_state.brake_pressed, (long)steering_tenths,
        (unsigned long)can_rx_count, (unsigned long)can_valid_rx_count,
        (unsigned long)can_speed_rx_count, (unsigned long)can_brake_rx_count,
        (unsigned long)can_steering_rx_count,
        (unsigned long)can_speed_last_ms, (unsigned long)can_brake_last_ms,
        (unsigned long)can_steering_last_ms,
        (unsigned long)can_rx_error_count, (unsigned long)can_invalid_frame_count,
        (unsigned long)can_fifo_lost_count, (unsigned long)can_bus_off,
        (unsigned long)can_last_protocol_error);
    if (length > 0 && (size_t)length < sizeof(telemetry_buffer) &&
        HAL_UART_Transmit_IT(&huart3, (uint8_t *)telemetry_buffer, (uint16_t)length) == HAL_OK)
    {
        telemetry_tx_count++;
    }
    else
    {
        telemetry_skip_count++;
    }
}

/* USER CODE END 0 */

int main(void)
{
    HAL_Init();
    SystemClock_Config();

    MX_GPIO_Init();
    MX_USART3_UART_Init();
    MX_FDCAN1_Init();

    /* USER CODE BEGIN 2 */
    if (HAL_UART_Receive_IT(&huart3, &rx_byte, 1) != HAL_OK)
    {
        Error_Handler();
    }

    CAN_Setup();
    can_last_test_ms = HAL_GetTick();
    /* USER CODE END 2 */

    /* Infinite loop */
    /* USER CODE BEGIN WHILE */
    while (1)
    {
        CAN_ProcessRx();

        /* Never send loopback dummy values onto the real bus. */
        if (hfdcan1.Init.Mode == FDCAN_MODE_INTERNAL_LOOPBACK &&
            (HAL_GetTick() - can_last_test_ms) >= CAN_TEST_PERIOD_MS)
        {
            can_last_test_ms = HAL_GetTick();
            CAN_SendTestData();
        }

        CAN_ProcessRx();
        CAN_CheckStatus();
        UART_SendVehicleTelemetry();
    }
    /* USER CODE END WHILE */
}

void SystemClock_Config(void)
{
    RCC_OscInitTypeDef RCC_OscInitStruct = {0};
    RCC_ClkInitTypeDef RCC_ClkInitStruct = {0};

    HAL_PWREx_ConfigSupply(PWR_LDO_SUPPLY);

    __HAL_PWR_VOLTAGESCALING_CONFIG(PWR_REGULATOR_VOLTAGE_SCALE3);

    while (!__HAL_PWR_GET_FLAG(PWR_FLAG_VOSRDY))
    {
    }

    RCC_OscInitStruct.OscillatorType = RCC_OSCILLATORTYPE_HSI;
    RCC_OscInitStruct.HSIState = RCC_HSI_DIV1;
    RCC_OscInitStruct.HSICalibrationValue = 64;
    RCC_OscInitStruct.PLL.PLLState = RCC_PLL_ON;
    RCC_OscInitStruct.PLL.PLLSource = RCC_PLLSOURCE_HSI;
    RCC_OscInitStruct.PLL.PLLM = 32;
    RCC_OscInitStruct.PLL.PLLN = 128;
    RCC_OscInitStruct.PLL.PLLP = 2;
    RCC_OscInitStruct.PLL.PLLQ = 2;
    RCC_OscInitStruct.PLL.PLLR = 2;
    RCC_OscInitStruct.PLL.PLLRGE = RCC_PLL1VCIRANGE_1;
    RCC_OscInitStruct.PLL.PLLVCOSEL = RCC_PLL1VCOWIDE;
    RCC_OscInitStruct.PLL.PLLFRACN = 0;

    if (HAL_RCC_OscConfig(&RCC_OscInitStruct) != HAL_OK)
    {
        Error_Handler();
    }

    RCC_ClkInitStruct.ClockType =
        RCC_CLOCKTYPE_HCLK |
        RCC_CLOCKTYPE_SYSCLK |
        RCC_CLOCKTYPE_PCLK1 |
        RCC_CLOCKTYPE_PCLK2 |
        RCC_CLOCKTYPE_D3PCLK1 |
        RCC_CLOCKTYPE_D1PCLK1;

    RCC_ClkInitStruct.SYSCLKSource = RCC_SYSCLKSOURCE_HSI;
    RCC_ClkInitStruct.SYSCLKDivider = RCC_SYSCLK_DIV1;
    RCC_ClkInitStruct.AHBCLKDivider = RCC_HCLK_DIV1;
    RCC_ClkInitStruct.APB3CLKDivider = RCC_APB3_DIV1;
    RCC_ClkInitStruct.APB1CLKDivider = RCC_APB1_DIV1;
    RCC_ClkInitStruct.APB2CLKDivider = RCC_APB2_DIV1;
    RCC_ClkInitStruct.APB4CLKDivider = RCC_APB4_DIV1;

    if (HAL_RCC_ClockConfig(
            &RCC_ClkInitStruct,
            FLASH_LATENCY_1) != HAL_OK)
    {
        Error_Handler();
    }
}

static void MX_FDCAN1_Init(void)
{
    hfdcan1.Instance = FDCAN1;
    hfdcan1.Init.FrameFormat = FDCAN_FRAME_CLASSIC;
    hfdcan1.Init.Mode = FDCAN_MODE_NORMAL;
    hfdcan1.Init.AutoRetransmission = DISABLE;
    hfdcan1.Init.TransmitPause = DISABLE;
    hfdcan1.Init.ProtocolException = DISABLE;

    hfdcan1.Init.NominalPrescaler = 4;
    hfdcan1.Init.NominalSyncJumpWidth = 1;
    hfdcan1.Init.NominalTimeSeg1 = 56;
    hfdcan1.Init.NominalTimeSeg2 = 7;

    hfdcan1.Init.DataPrescaler = 1;
    hfdcan1.Init.DataSyncJumpWidth = 1;
    hfdcan1.Init.DataTimeSeg1 = 1;
    hfdcan1.Init.DataTimeSeg2 = 1;

    hfdcan1.Init.MessageRAMOffset = 0;
    hfdcan1.Init.StdFiltersNbr = 1;
    hfdcan1.Init.ExtFiltersNbr = 0;

    hfdcan1.Init.RxFifo0ElmtsNbr = 3;
    hfdcan1.Init.RxFifo0ElmtSize = FDCAN_DATA_BYTES_8;

    hfdcan1.Init.RxFifo1ElmtsNbr = 0;
    hfdcan1.Init.RxFifo1ElmtSize = FDCAN_DATA_BYTES_8;

    hfdcan1.Init.RxBuffersNbr = 0;
    hfdcan1.Init.RxBufferSize = FDCAN_DATA_BYTES_8;

    hfdcan1.Init.TxEventsNbr = 0;
    hfdcan1.Init.TxBuffersNbr = 0;
    hfdcan1.Init.TxFifoQueueElmtsNbr = 3;
    hfdcan1.Init.TxFifoQueueMode = FDCAN_TX_FIFO_OPERATION;
    hfdcan1.Init.TxElmtSize = FDCAN_DATA_BYTES_8;

    if (HAL_FDCAN_Init(&hfdcan1) != HAL_OK)
    {
        Error_Handler();
    }
}

static void MX_USART3_UART_Init(void)
{
    huart3.Instance = USART3;
    huart3.Init.BaudRate = 115200;
    huart3.Init.WordLength = UART_WORDLENGTH_8B;
    huart3.Init.StopBits = UART_STOPBITS_1;
    huart3.Init.Parity = UART_PARITY_NONE;
    huart3.Init.Mode = UART_MODE_TX_RX;
    huart3.Init.HwFlowCtl = UART_HWCONTROL_NONE;
    huart3.Init.OverSampling = UART_OVERSAMPLING_16;
    huart3.Init.OneBitSampling = UART_ONE_BIT_SAMPLE_DISABLE;
    huart3.Init.ClockPrescaler = UART_PRESCALER_DIV1;
    huart3.AdvancedInit.AdvFeatureInit = UART_ADVFEATURE_NO_INIT;

    if (HAL_UART_Init(&huart3) != HAL_OK)
    {
        Error_Handler();
    }

    if (HAL_UARTEx_SetTxFifoThreshold(
            &huart3,
            UART_TXFIFO_THRESHOLD_1_8) != HAL_OK)
    {
        Error_Handler();
    }

    if (HAL_UARTEx_SetRxFifoThreshold(
            &huart3,
            UART_RXFIFO_THRESHOLD_1_8) != HAL_OK)
    {
        Error_Handler();
    }

    if (HAL_UARTEx_DisableFifoMode(&huart3) != HAL_OK)
    {
        Error_Handler();
    }
}

static void MX_GPIO_Init(void)
{
    GPIO_InitTypeDef GPIO_InitStruct = {0};

    __HAL_RCC_GPIOB_CLK_ENABLE();
    __HAL_RCC_GPIOD_CLK_ENABLE();
    __HAL_RCC_GPIOA_CLK_ENABLE();

    HAL_GPIO_WritePin(GPIOB, GPIO_PIN_0, GPIO_PIN_RESET);

    GPIO_InitStruct.Pin = GPIO_PIN_0;
    GPIO_InitStruct.Mode = GPIO_MODE_OUTPUT_PP;
    GPIO_InitStruct.Pull = GPIO_NOPULL;
    GPIO_InitStruct.Speed = GPIO_SPEED_FREQ_LOW;

    HAL_GPIO_Init(GPIOB, &GPIO_InitStruct);
}

void Error_Handler(void)
{
    __disable_irq();

    while (1)
    {
    }
}

#ifdef USE_FULL_ASSERT
void assert_failed(uint8_t *file, uint32_t line)
{
    (void)file;
    (void)line;
}
#endif
